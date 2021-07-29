import argparse
import io
import json
import logging
import math
import pathlib
import sys
import tempfile
import shutil
import traceback
import datetime
import docker
import redis
import os
from pathlib import Path
from zipfile import ZipFile, ZipInfo

from redisbench_admin.environments.oss_standalone import (
    spin_up_local_redis,
    generate_standalone_redis_server_args,
)
from redisbench_admin.run.common import (
    get_start_time_vars,
    prepare_benchmark_parameters,
)
from redisbench_admin.run.run import calculate_benchmark_duration_and_check
from redisbench_admin.run_local.local_helpers import (
    check_benchmark_binaries_local_requirements,
)
from redisbench_admin.utils.benchmark_config import (
    extract_redis_dbconfig_parameters,
    get_final_benchmark_config,
)
from redisbench_admin.utils.local import is_process_alive, get_local_run_full_filename
from redisbench_admin.utils.results import post_process_benchmark_results

from redis_benchmarks_specification.__builder__.schema import get_build_config
from redis_benchmarks_specification.__common__.env import (
    STREAM_KEYNAME_GH_EVENTS_COMMIT,
    GH_REDIS_SERVER_HOST,
    GH_REDIS_SERVER_PORT,
    GH_REDIS_SERVER_AUTH,
    LOG_FORMAT,
    LOG_DATEFMT,
    LOG_LEVEL,
    SPECS_PATH_SETUPS,
    STREAM_GH_EVENTS_COMMIT_BUILDERS_CG,
    STREAM_KEYNAME_NEW_BUILD_EVENTS,
    SPECS_PATH_TEST_SUITES,
    GH_REDIS_SERVER_USER,
    STREAM_GH_NEW_BUILD_RUNNERS_CG,
    MACHINE_CPU_COUNT,
)
from redis_benchmarks_specification.__common__.package import PACKAGE_DIR
from redis_benchmarks_specification.__setups__.topologies import get_topologies


def main():
    parser = argparse.ArgumentParser(
        description="redis-benchmarks-spec runner(self-contained)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--cpu-count",
        type=int,
        default=MACHINE_CPU_COUNT,
        help="Specify how much of the available CPU resources the coordinator can use.",
    )
    parser.add_argument(
        "--logname", type=str, default=None, help="logname to write the logs to"
    )
    parser.add_argument(
        "--consumer-start-id",
        type=str,
        default=">",
    )
    parser.add_argument(
        "--setups-folder",
        type=str,
        default=SPECS_PATH_SETUPS,
        help="Setups folder, containing the build environment variations sub-folder that we use to trigger different build artifacts",
    )
    parser.add_argument(
        "--test-suites-folder",
        type=str,
        default=SPECS_PATH_TEST_SUITES,
        help="Test suites folder, containing the different test variations",
    )
    args = parser.parse_args()

    if args.logname is not None:
        print("Writting log to {}".format(args.logname))
        logging.basicConfig(
            filename=args.logname,
            filemode="a",
            format=LOG_FORMAT,
            datefmt=LOG_DATEFMT,
            level=LOG_LEVEL,
        )
    else:
        # logging settings
        logging.basicConfig(
            format=LOG_FORMAT,
            level=LOG_LEVEL,
            datefmt=LOG_DATEFMT,
        )
    logging.info("Using package dir {} for inner file paths".format(PACKAGE_DIR))
    topologies_folder = os.path.abspath(
        PACKAGE_DIR + "/" + args.setups_folder + "/topologies"
    )
    topologies_files = pathlib.Path(topologies_folder).glob("*.yml")
    topologies_files = [str(x) for x in topologies_files]
    logging.info(
        "Reading topologies specifications from: {}".format(
            " ".join([str(x) for x in topologies_files])
        )
    )
    topologies_map = get_topologies(topologies_files[0])

    testsuites_folder = os.path.abspath(PACKAGE_DIR + "/" + args.test_suites_folder)
    logging.info(
        "Using redis available at: {}:{} to read the event streams".format(
            GH_REDIS_SERVER_HOST, GH_REDIS_SERVER_PORT
        )
    )
    try:
        conn = redis.StrictRedis(
            host=GH_REDIS_SERVER_HOST,
            port=GH_REDIS_SERVER_PORT,
            decode_responses=False,  # dont decode due to binary archives
            password=GH_REDIS_SERVER_AUTH,
            username=GH_REDIS_SERVER_USER,
        )
        conn.ping()
    except redis.exceptions.ConnectionError as e:
        logging.error(
            "Unable to connect to redis available at: {}:{} to read the event streams".format(
                GH_REDIS_SERVER_HOST, GH_REDIS_SERVER_PORT
            )
        )
        logging.error("Error message {}".format(e.__str__()))
        exit(1)

    logging.info("checking build spec requirements")
    try:
        conn.xgroup_create(
            STREAM_KEYNAME_NEW_BUILD_EVENTS,
            STREAM_GH_NEW_BUILD_RUNNERS_CG,
            mkstream=True,
        )
        logging.info(
            "Created consumer group named {} to distribute work.".format(
                STREAM_GH_NEW_BUILD_RUNNERS_CG
            )
        )
    except redis.exceptions.ResponseError as e:
        logging.info(
            "Consumer group named {} already existed.".format(
                STREAM_GH_NEW_BUILD_RUNNERS_CG
            )
        )
    previous_id = None
    docker_client = docker.from_env()
    home = str(Path.home())
    availabe_cpus = args.cpu_count

    while True:
        logging.info("Entering blocking read waiting for work.")
        if previous_id is None:
            previous_id = args.consumer_start_id
        newTestInfo = conn.xreadgroup(
            STREAM_GH_NEW_BUILD_RUNNERS_CG,
            "{}-self-contained-proc#{}".format(STREAM_GH_NEW_BUILD_RUNNERS_CG, "1"),
            {STREAM_KEYNAME_NEW_BUILD_EVENTS: previous_id},
            count=1,
            block=0,
        )
        if len(newTestInfo[0]) < 2 or len(newTestInfo[0][1]) < 1:
            previous_id = ">"
            continue
        streamId, testDetails = newTestInfo[0][1][0]
        previous_id = streamId.decode()
        logging.info("Received work . Stream id {}.".format(streamId))

        if b"git_hash" in testDetails:
            git_hash = testDetails[b"git_hash"]
            logging.info("Received commit hash specifier {}.".format(git_hash))
            build_artifacts_str = "redis-server"
            build_image = testDetails[b"build_image"].decode()
            run_image = build_image
            if b"run_image" in testDetails[b"run_image"]:
                run_image = testDetails[b"run_image"].decode()
            if b"build_artifacts" in testDetails:
                build_artifacts_str = testDetails[b"build_artifacts"].decode()
            build_artifacts = build_artifacts_str.split(",")

            files = pathlib.Path(testsuites_folder).glob("*.yml")
            files = [str(x) for x in files]
            logging.info(
                "Running all specified benchmarks: {}".format(
                    " ".join([str(x) for x in files])
                )
            )

            for test_file in files:
                redis_containers = []
                client_containers = []

                with open(test_file, "r") as stream:
                    benchmark_config, test_name = get_final_benchmark_config(
                        None, stream, ""
                    )

                    (
                        redis_configuration_parameters,
                        _,
                    ) = extract_redis_dbconfig_parameters(benchmark_config, "dbconfig")
                    for topology_spec_name in benchmark_config["redis-topologies"]:
                        try:
                            current_cpu_pos = 0
                            previous_cpu_pos = current_cpu_pos
                            topology_spec = topologies_map[topology_spec_name]
                            db_cpu_limit = topology_spec["resources"]["requests"][
                                "cpus"
                            ]
                            ceil_db_cpu_limit = math.ceil(float(db_cpu_limit))
                            current_cpu_pos = current_cpu_pos + int(ceil_db_cpu_limit)
                            temporary_dir = tempfile.mkdtemp(dir=home)
                            logging.info(
                                "Using local temporary dir to persist redis build artifacts. Path: {}".format(
                                    temporary_dir
                                )
                            )
                            redis_server_path = None
                            benchmark_tool = "redis-benchmark"
                            for build_artifact in build_artifacts:
                                buffer = testDetails[
                                    bytes("{}".format(build_artifact).encode())
                                ]
                                artifact_fname = "{}/{}".format(
                                    temporary_dir, build_artifact
                                )
                                with open(artifact_fname, "wb") as fd:
                                    fd.write(buffer)
                                    os.chmod(artifact_fname, 755)
                                if build_artifact == "redis-server":
                                    redis_server_path = artifact_fname

                                logging.info(
                                    "Successfully restored {} into {}".format(
                                        build_artifact, artifact_fname
                                    )
                                )
                            port = 6379
                            mnt_point = "/mnt/redis/"
                            command = generate_standalone_redis_server_args(
                                "{}redis-server".format(mnt_point),
                                port,
                                mnt_point,
                                redis_configuration_parameters,
                            )
                            command_str = " ".join(command)
                            db_cpuset_cpus = ",".join([str(x) for x in range(previous_cpu_pos, current_cpu_pos) ]                            )
                            logging.info(
                                "Running redis-server on docker image {} (cpuset={}) with the following args: {}".format(
                                    run_image, db_cpuset_cpus, command_str
                                )
                            )
                            container = docker_client.containers.run(
                                image=run_image,
                                volumes={
                                    temporary_dir: {"bind": mnt_point, "mode": "rw"},
                                },
                                auto_remove=True,
                                privileged=True,
                                working_dir=mnt_point,
                                command=command_str,
                                network_mode="host",
                                detach=True,
                                cpuset_cpus=db_cpuset_cpus,
                            )
                            redis_containers.append(container)

                            full_benchmark_path = "/usr/local/bin/redis-benchmark"
                            client_mnt_point = "/mnt/client/"
                            benchmark_tool_workdir = client_mnt_point

                            # setup the benchmark
                            (
                                start_time,
                                start_time_ms,
                                start_time_str,
                            ) = get_start_time_vars()
                            local_benchmark_output_filename = (
                                get_local_run_full_filename(
                                    start_time_str,
                                    git_hash,
                                    test_name,
                                    "oss-standalone",
                                )
                            )
                            logging.info(
                                "Will store benchmark json output to local file {}".format(
                                    local_benchmark_output_filename
                                )
                            )

                            # prepare the benchmark command
                            (
                                benchmark_command,
                                benchmark_command_str,
                            ) = prepare_benchmark_parameters(
                                benchmark_config,
                                full_benchmark_path,
                                port,
                                "localhost",
                                local_benchmark_output_filename,
                                False,
                                benchmark_tool_workdir,
                                False,
                            )
                            r = redis.StrictRedis(port=6379)
                            r.ping()

                            # run the benchmark
                            benchmark_start_time = datetime.datetime.now()

                            client_container_stdout = docker_client.containers.run(
                                image="redis:6.2.4",
                                volumes={
                                    temporary_dir: {
                                        "bind": client_mnt_point,
                                        "mode": "rw",
                                    },
                                },
                                auto_remove=True,
                                privileged=True,
                                working_dir=benchmark_tool_workdir,
                                command=benchmark_command_str,
                                network_mode="host",
                                detach=False,
                            )
                            benchmark_end_time = datetime.datetime.now()
                            benchmark_duration_seconds = (
                                calculate_benchmark_duration_and_check(
                                    benchmark_end_time, benchmark_start_time
                                )
                            )
                            logging.info("output {}".format(client_container_stdout))
                            r.shutdown(save=False)

                            post_process_benchmark_results(
                                benchmark_tool,
                                local_benchmark_output_filename,
                                start_time_ms,
                                start_time_str,
                                client_container_stdout,
                                None,
                            )

                            with open(
                                local_benchmark_output_filename, "r"
                            ) as json_file:
                                results_dict = json.load(json_file)

                            if args.push_results_redistimeseries:
                                logging.info("Pushing results to RedisTimeSeries.")
                                # redistimeseries_results_logic(
                                #     artifact_version,
                                #     benchmark_config,
                                #     default_metrics,
                                #     deployment_type,
                                #     exporter_timemetric_path,
                                #     results_dict,
                                #     rts,
                                #     test_name,
                                #     tf_github_branch,
                                #     tf_github_org,
                                #     tf_github_repo,
                                #     tf_triggering_env,
                                # )
                                try:
                                    rts.redis.sadd(testcases_setname, test_name)
                                    rts.incrby(
                                        tsname_project_total_success,
                                        1,
                                        timestamp=start_time_ms,
                                        labels=get_project_ts_tags(
                                            tf_github_org,
                                            tf_github_repo,
                                            deployment_type,
                                            tf_triggering_env,
                                        ),
                                    )
                                    #
                                except redis.exceptions.ResponseError as e:
                                    logging.warning(
                                        "Error while updating secondary data structures {}. ".format(
                                            e.__str__()
                                        )
                                    )
                                    pass

                        except:
                            logging.critical(
                                "Some unexpected exception was caught "
                                "during local work. Failing test...."
                            )
                            logging.critical(sys.exc_info()[0])
                            print("-" * 60)
                            traceback.print_exc(file=sys.stdout)
                            print("-" * 60)
                        # tear-down
                        logging.info("Tearing down setup")
                        for container in redis_containers:
                            container.stop()
                        for container in client_containers:
                            if type(container) != bytes:
                                container.stop()
                        shutil.rmtree(temporary_dir, ignore_errors=True)

        else:
            logging.error("Missing commit information within received message.")
            continue


def generate_standalone_redis_server_args(
    binary, port, dbdir, configuration_parameters=None
):
    added_params = ["port", "protected-mode", "dir"]
    # start redis-server
    command = [
        binary,
        "--protected-mode",
        "no",
        "--port",
        "{}".format(port),
        "--dir",
        dbdir,
    ]
    if configuration_parameters is not None:
        for parameter, parameter_value in configuration_parameters.items():
            if parameter not in added_params:
                command.extend(
                    [
                        "--{}".format(parameter),
                        parameter_value,
                    ]
                )
    return command
