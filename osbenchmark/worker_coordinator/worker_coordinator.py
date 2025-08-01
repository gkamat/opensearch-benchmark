# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.
# Licensed to Elasticsearch B.V. under one or more contributor
# license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright
# ownership. Elasticsearch B.V. licenses this file to you under
# the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# 	http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import asyncio
import collections
import concurrent.futures
import configparser
import datetime
import itertools
import json
import logging
import math
import multiprocessing
import os
import queue
import random
import sys
import threading
from dataclasses import dataclass
from typing import Callable, List, Dict, Any

import time
from enum import Enum

import thespian.actors

from osbenchmark import actor, config, exceptions, metrics, workload, client, paths, PROGRAM_NAME, telemetry
from osbenchmark.worker_coordinator import runner, scheduler
from osbenchmark.workload import WorkloadProcessorRegistry, load_workload, load_workload_plugins
from osbenchmark.utils import convert, console, net
from osbenchmark.worker_coordinator.errors import parse_error
##################################
#
# Messages sent between worker_coordinators
#
##################################
class PrepareBenchmark:
    """
    Initiates preparation steps for a benchmark. The benchmark should only be started after StartBenchmark is sent.
    """

    def __init__(self, config, workload):
        """
        :param config: OSB internal configuration object.
        :param workload: The workload to use.
        """
        self.config = config
        self.workload = workload


class StartBenchmark:
    pass


class PrepareWorkload:
    """
    Initiates preparation of a workload.

    """
    def __init__(self, cfg, workload):
        """
        :param cfg: OSB internal configuration object.
        :param workload: The workload to use.
        """
        self.config = cfg
        self.workload = workload


class WorkloadPrepared:
    pass


class StartTaskLoop:
    def __init__(self, workload_name, cfg):
        self.workload_name = workload_name
        self.cfg = cfg


class DoTask:
    def __init__(self, task, cfg):
        self.task = task
        self.cfg = cfg


@dataclass(frozen=True)
class WorkerTask:
    """
    Unit of work that should be completed by the low-level TaskExecutionActor
    """
    func: Callable
    params: dict


class ReadyForWork:
    pass


class WorkerIdle:
    pass


class PreparationComplete:
    def __init__(self, distribution_flavor, distribution_version, revision):
        self.distribution_flavor = distribution_flavor
        self.distribution_version = distribution_version
        self.revision = revision


class StartWorker:
    """
    Starts a worker.
    """

    def __init__(self, worker_id, config, workload, client_allocations, feedback_actor=None, error_queue=None, queue_lock=None, shared_states=None):
        """
        :param worker_id: Unique (numeric) id of the worker.
        :param config: OSB internal configuration object.
        :param workload: The workload to use.
        :param client_allocations: A structure describing which clients need to run which tasks.
        """
        self.worker_id = worker_id
        self.config = config
        self.workload = workload
        self.client_allocations = client_allocations
        self.feedback_actor = feedback_actor
        self.error_queue = error_queue
        self.queue_lock = queue_lock
        self.shared_states = shared_states


class Drive:
    """
    Tells a load generator to drive (either after a join point or initially).
    """

    def __init__(self, client_start_timestamp):
        self.client_start_timestamp = client_start_timestamp


class CompleteCurrentTask:
    """
    Tells a load generator to prematurely complete its current task.
    This is used to model task dependencies for parallel tasks (i.e. if a
    specific task that is marked accordingly in the workload finishes,
    it will also signal termination of all other tasks in the same parallel
    element).
    """


class UpdateSamples:
    """
    Used to send samples from a load generator node to the master.
    """

    def __init__(self, client_id, samples, profile_samples):
        self.client_id = client_id
        self.samples = samples
        self.profile_samples = profile_samples


class JoinPointReached:
    """
    Tells the master that a load generator has reached a join point. Used for coordination across multiple load generators.
    """

    def __init__(self, worker_id, task):
        self.worker_id = worker_id
        # Using perf_counter here is fine even in the distributed case. Although we "leak" this value to other
        # machines, we will only ever interpret this value on the same machine (see `Drive` and the implementation
        # in `WorkerCoordinator#joinpoint_reached()`).
        self.worker_timestamp = time.perf_counter()
        self.task = task


class BenchmarkComplete:
    """
    Indicates that the benchmark is complete.
    """

    def __init__(self, metrics):
        self.metrics = metrics


class TaskFinished:
    def __init__(self, metrics, next_task_scheduled_in):
        self.metrics = metrics
        self.next_task_scheduled_in = next_task_scheduled_in

def load_redline_config():
    config = configparser.ConfigParser()
    benchmark_home = os.environ.get('BENCHMARK_HOME') or os.environ['HOME']
    benchmark_ini = benchmark_home + '/.benchmark/benchmark.ini'
    if not os.path.isfile(benchmark_ini):
        console.println(f"WARNING: redline config file {benchmark_ini} not found. Proceeding with default values.")
        return {}

    config.read(benchmark_ini)
    config_object = {}

    if "redline" in config:
        redline = config["redline"]
        for key in [
            "scale_step",
            "scaledown_percentage",
            "post_scaledown_sleep",
            "max_cpu_usage",
            "cpu_window_seconds",
            "cpu_check_interval",
            "max_clients"
        ]:
            if key in redline:
                config_object[key] = redline[key]

    return config_object

class ConfigureFeedbackScaling:
    DEFAULT_SLEEP_SECONDS = 30
    DEFAULT_SCALE_STEP = 5
    DEFAULT_SCALEDOWN_PCT = 0.10
    DEFAULT_CPU_WINDOW_SECONDS = 30
    DEFAULT_CPU_CHECK_INTERVAL = 30

    def __init__(self, scale_step=None, scale_down_pct=None, sleep_seconds=None, max_clients=None, cpu_max=None,
                cpu_window_seconds=None, cpu_check_interval=None, metrics_index=None, test_execution_id=None, cfg=None):

        config_object = load_redline_config()

        # priority: command flags -> config object -> default values
        self.scale_step = int(scale_step if scale_step is not None else config_object.get("scale_step", self.DEFAULT_SCALE_STEP))
        self.scale_down_pct = float(scale_down_pct if scale_down_pct is not None else config_object.get("scaledown_percentage", self.DEFAULT_SCALEDOWN_PCT))
        self.sleep_seconds = int(sleep_seconds if sleep_seconds is not None else config_object.get("post_scaledown_sleep", self.DEFAULT_SLEEP_SECONDS))
        self.cpu_window_seconds = int(cpu_window_seconds if cpu_window_seconds is not None else config_object.get("cpu_window_seconds", self.DEFAULT_CPU_WINDOW_SECONDS))
        self.cpu_check_interval = int(cpu_check_interval if cpu_check_interval is not None else config_object.get("cpu_check_interval", self.DEFAULT_CPU_CHECK_INTERVAL))
        self.max_clients = max_clients
        self.cpu_max=cpu_max
        self.cfg=cfg
        self.metrics_index=metrics_index
        self.test_execution_id=test_execution_id

class EnableFeedbackScaling:
    pass

class DisableFeedbackScaling:
    pass

class FeedbackState(Enum):
    """Various states for the FeedbackActor"""
    NEUTRAL = "neutral"
    SCALING_DOWN = "scaling_down"
    SLEEP = "sleep"
    SCALING_UP = "scaling_up"
    DISABLED = "disabled"

class StartFeedbackActor:
    def __init__(self, error_queue=None, queue_lock=None, shared_states=None):
        self.shared_states = shared_states
        self.error_queue = error_queue
        self.queue_lock = queue_lock

# pylint: disable=too-many-public-methods
class FeedbackActor(actor.BenchmarkActor):
    POST_SCALEDOWN_SECONDS = 30
    WAKEUP_INTERVAL = 1

    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.state = FeedbackState.DISABLED
        self.shared_client_states = {}
        self.workers_reported = 0
        self.total_client_count = 0
        self.total_active_client_count = 0  # must be tracked for scaling up/down
        self.num_clients_to_scale_up = 5
        self.percentage_clients_to_scale_down = 0.10
        self.sleep_start_time = time.perf_counter()
        self.last_error_time = time.perf_counter() - FeedbackActor.POST_SCALEDOWN_SECONDS
        self.last_scaleup_time = time.perf_counter() - FeedbackActor.POST_SCALEDOWN_SECONDS
        self.max_stable_clients = 0 # the value we want to return at the end of the test
        # These will be passed in via StartFeedbackActor:
        self.error_queue = None
        self.queue_lock = None
        self.max_error_threshold = 10000
        # Probing configuration
        self.probe_probability = 0.05
        self.probe_interval = 10
        self._cycles_since_probe = 0
        # for cpu based feedback
        self.last_cpu_check = time.perf_counter()
        self.max_cpu_threshold = None
        self.cpu_window_seconds = None
        self.cpu_check_interval = None
        # client, index, and test execution ID for querying users' data store
        self.os_client = None
        self.metrics_index = None
        self.test_execution_id=None

    def receiveMsg_StartFeedbackActor(self, msg, sender) -> None:
        """
        Initializes the FeedbackActor with expected worker count, client dictionaries, error queue, and queue lock.
        """
        self.shared_client_states = msg.shared_states
        self.total_client_count = sum(len(state) for state in self.shared_client_states.values())
        self.error_queue = msg.error_queue
        self.queue_lock = msg.queue_lock
        self.wakeupAfter(datetime.timedelta(seconds=FeedbackActor.WAKEUP_INTERVAL))

    def receiveMsg_WakeupMessage(self, msg, sender) -> None:
        # Check state and re-schedule wakeups.
        self.handle_state()
        self.wakeupAfter(datetime.timedelta(seconds=FeedbackActor.WAKEUP_INTERVAL))

    def receiveUnrecognizedMessage(self, msg, sender) -> None:
        self.logger.info("Received unrecognized message: %s", msg)

    def receiveMsg_EnableFeedbackScaling(self, msg, sender):
        self.logger.info("FeedbackActor: scaling enabled.")
        self.max_error_threshold = 10000
        self._cycles_since_probe = 0
        self.state = FeedbackState.SCALING_UP

    def receiveMsg_DisableFeedbackScaling(self, msg, sender):
        self.logger.info("FeedbackActor: scaling disabled.")
        self.state = FeedbackState.DISABLED

    def receiveMsg_ConfigureFeedbackScaling(self, msg, sender):
        self.num_clients_to_scale_up = msg.scale_step
        self.percentage_clients_to_scale_down = msg.scale_down_pct
        self.POST_SCALEDOWN_SECONDS = msg.sleep_seconds
        # CPU feedback related items
        self.cpu_window_seconds = msg.cpu_window_seconds
        self.cpu_check_interval = msg.cpu_check_interval
        self.test_execution_id=msg.test_execution_id
        self.cfg=msg.cfg
        self.metrics_index = msg.metrics_index
        if msg.cpu_max:
            self.max_cpu_threshold = msg.cpu_max
            # create a new client to query the datastore for CPU based feedback
            # we can't pass the original metrics_store object from the WorkerCoordinator since it can't be pickled in a thespianpy message
            try:
                self.os_client = metrics.OsClientFactory(self.cfg).create()
            except Exception:
                raise exceptions.SystemSetupError("OS Client could not be created for redline testing. Ensure you are passing the correct config for your metrics store.")
        self.logger.info(
        "Feedback actor has received the following configuration: Max clients = %s, scale step = %d, scale down percentage = %f, sleep time = %d",
        self.total_client_count, self.num_clients_to_scale_up, self.percentage_clients_to_scale_down, self.POST_SCALEDOWN_SECONDS
        )

    def receiveMsg_ActorExitRequest(self, msg, sender):
        console.info("Redline test finished. Maximum stable client number reached: %d" % self.total_active_client_count)
        self.logger.info("FeedbackActor received ActorExitRequest and will shutdown")
        if hasattr(self, 'shared_client_states'):
            self.shared_client_states.clear()

    def receiveMsg_ResetErrorThreshold(self, msg, sender):
        """Reset the max error threshold to allow scaling up again."""
        self.max_error_threshold = float('inf')
        self.logger.info("Error threshold has been reset, allowing full scale-up")

    def check_for_errors(self) -> List[Dict[str, Any]]:
        """Poll the error queue for errors."""
        errors = []
        try:
            while True:
                error = self.error_queue.get_nowait()
                errors.append(error)
        except queue.Empty:
            pass  # queue is empty
        return errors

    def clear_queue(self) -> None:
        """Clear any remaining items from the error queue."""
        while True:
            try:
                self.error_queue.get_nowait()
            except queue.Empty:
                break

    def handle_state(self) -> None:
        current_time = time.perf_counter()
        # check CPU usage every N seconds
        if (self.max_cpu_threshold and current_time - self.last_cpu_check >= self.cpu_check_interval):
            self._check_cpu_usage()
            self.last_cpu_check = current_time
        errors = self.check_for_errors()

        sys.stdout.write("\x1b[s")               # Save cursor position
        sys.stdout.write("\x1b[1B")              # Move cursor down 1 line
        sys.stdout.write("\r\x1b[2K")            # Clear the line
        sys.stdout.write(f"[Redline] Active clients: {self.total_active_client_count}")
        sys.stdout.write("\x1b[u")               # Restore cursor position
        sys.stdout.flush()

        if self.state == FeedbackState.DISABLED:
            return

        if self.state == FeedbackState.SLEEP:
            if current_time - self.sleep_start_time >= self.POST_SCALEDOWN_SECONDS:
                self.logger.info("Sleep period complete, returning to NEUTRAL state")
                self.state = FeedbackState.NEUTRAL
                self.sleep_start_time = current_time
            return
        if errors:
            self.logger.info("Error messages detected, scaling down...")
            self.state = FeedbackState.SCALING_DOWN
            with self.queue_lock:  # Block producers while scaling down.
                self.scale_down()
            self.logger.info("Clients scaled down. Active clients: %d", self.total_active_client_count)
            self.last_error_time = current_time
            return

        if self.state == FeedbackState.NEUTRAL:
            self.max_stable_clients = max(self.max_stable_clients, self.total_active_client_count) # update the max number of stable clients
            if (current_time - self.last_error_time >= self.POST_SCALEDOWN_SECONDS and
                current_time - self.last_scaleup_time >= self.WAKEUP_INTERVAL):
                self.logger.info("No errors in the last %d seconds, scaling up", self.POST_SCALEDOWN_SECONDS)
                self.state = FeedbackState.SCALING_UP
            return

        if self.state == FeedbackState.SCALING_UP:
            self.logger.info("Scaling up...")
            self.scale_up()
            self.logger.info("Clients scaled up. Active clients: %d", self.total_active_client_count)
            self.state = FeedbackState.NEUTRAL
            return

    def scale_down(self) -> None:
        try:
            self.max_error_threshold = self.total_active_client_count
            self.logger.info("New max error threshold: %d", self.max_error_threshold)
            clients_to_pause = math.ceil(self.total_active_client_count * self.percentage_clients_to_scale_down)
            if clients_to_pause <= 0:
                self.logger.info("No clients to pause during scale down")
                return

            # Create a flattened list of (worker_id, client_id) tuples for all active clients
            all_active_clients = []
            for worker_id, client_states in self.shared_client_states.items():
                for client_id, status in client_states.items():
                    if status:  # Only include active clients
                        all_active_clients.append((worker_id, client_id))

            # If we need to pause more clients than are active, adjust the count
            clients_to_pause = min(clients_to_pause, len(all_active_clients))

            # Select clients to pause - randomly sample for better distribution
            clients_to_pause_indices = random.sample(range(len(all_active_clients)), clients_to_pause)
            clients_to_pause_list = [all_active_clients[i] for i in clients_to_pause_indices]

            # Pause the selected clients in a single pass
            for worker_id, client_id in clients_to_pause_list:
                self.shared_client_states[worker_id][client_id] = False
                self.total_active_client_count -= 1

            self.logger.info("Scaling down complete. Paused %d clients", clients_to_pause)
        finally:
            self.state = FeedbackState.SLEEP
            self.clear_queue()
            self.sleep_start_time = self.last_scaleup_time = time.perf_counter()

    def scale_up(self) -> None:
        try:
            self.logger.info("Max error threshold: %d", self.max_error_threshold)
            gap = self.max_error_threshold - self.total_active_client_count
            max_clients_to_add = min(self.num_clients_to_scale_up, gap)
            self.logger.info("Max clients to add: %d", max_clients_to_add)

            if max_clients_to_add <= 0:
                probe = False
                if random.random() < self.probe_probability:
                    probe = True
                self._cycles_since_probe += 1
                if self._cycles_since_probe >= self.probe_interval:
                    probe = True
                    self._cycles_since_probe = 0
                if probe:
                    self.logger.info("Probing above ceiling %s; forcing 1 extra client", self.max_error_threshold)
                    max_clients_to_add = 1
                else:
                    self.logger.debug("Ceiling reached; skipping scale_up")
                    return

            clients_activated = 0
            inactive_clients = [
                (worker_id, client_id)
                for worker_id, client_states in self.shared_client_states.items()
                for client_id, active in client_states.items()
                if not active
            ]

            random.shuffle(inactive_clients)

            for worker_id, client_id in inactive_clients:
                if clients_activated >= max_clients_to_add:
                    break
                self.shared_client_states[worker_id][client_id] = True
                self.total_active_client_count += 1
                clients_activated += 1
                self.logger.info("Unpaused client %d on worker %d", client_id, worker_id)

            if clients_activated < max_clients_to_add:
                self.logger.info("Not enough inactive clients to activate. Activated %d clients", clients_activated)

        finally:
            self.last_scaleup_time = time.perf_counter()
            self.state = FeedbackState.NEUTRAL

    def _check_cpu_usage(self):
        """
        Grab the average CPU load per-node in the past N seconds
        If any exceed the threshold given, report to the error queue
        """
        body = {
            "size": 0,
            "query": {
                "bool": {
                "filter": [
                    { "term":  { "name": "node-stats" }},
                    { "term":  { "test-execution-id": self.test_execution_id }},
                    { "range": { "@timestamp": { "gte": f"now-{self.cpu_window_seconds}s", "lte": "now" }}}
                ]
                }
            },
            "aggs": {
                "nodes": {
                "terms": {
                    "field": "meta.node_name",
                    "size": 1000
                },
                "aggs": {
                    "avg_cpu": {
                    "avg": { "field": "process_cpu_percent" }
                    },
                    "hot_node_filter": {
                    "bucket_selector": {
                        "buckets_path": { "avgCpu": "avg_cpu" },
                        "script": f"params.avgCpu > {self.max_cpu_threshold}"
                    }
                    }
                }
                }
            }
        }
        resp = self.os_client.search(index=self.metrics_index, body=body)
        buckets = resp['aggregations']['nodes']['buckets']
        if buckets:
            for bucket in buckets:
                self.logger.info("Node %s avg CPU=%.1f%% > threshold %.1f%%", bucket['key'], bucket['avg_cpu']['value'], self.max_cpu_threshold)
                try:
                    self.error_queue.put_nowait({
                        "type":       "cpu_threshold_exceeded",
                        "node_name":  bucket['key'],
                        "value":      bucket['avg_cpu']['value']
                    })
                except queue.Full:
                    self.logger.warning("Error queue full; dropping cpu_threshold_exceeded for node %s", bucket['key'])
                break # we only need one error message to trigger a scaledown
        else:
            self.logger.info("All nodes are currently under max usage threshold")

class WorkerCoordinatorActor(actor.BenchmarkActor):
    RESET_RELATIVE_TIME_MARKER = "reset_relative_time"

    WAKEUP_INTERVAL_SECONDS = 1

    # post-process request metrics every N seconds and send it to the metrics store
    POST_PROCESS_INTERVAL_SECONDS = 30

    """
    Coordinates all workers. This is actually only a thin actor wrapper layer around ``WorkerCoordinator`` which does the actual work.
    """

    def __init__(self):
        super().__init__()
        self.start_sender = None
        self.coordinator = None
        self.status = "init"
        self.post_process_timer = 0
        self.cluster_details = None
        self.feedback_actor = None
        self.worker_shared_states = {}

    def receiveMsg_PoisonMessage(self, poisonmsg, sender):
        self.logger.error("Main worker_coordinator received a fatal indication from load generator (%s). Shutting down.", poisonmsg.details)
        self.coordinator.close()
        self.send(self.start_sender, actor.BenchmarkFailure("Fatal workload or load generator indication", poisonmsg.details))

    def receiveMsg_BenchmarkFailure(self, msg, sender):
        self.logger.error("Main worker_coordinator received a fatal exception from load generator. Shutting down.")
        self.coordinator.close()
        self.send(self.start_sender, msg)

    def receiveMsg_BenchmarkCancelled(self, msg, sender):
        self.logger.info("Main worker_coordinator received a notification that the benchmark has been cancelled.")
        self.coordinator.close()
        # shut down FeedbackActor if it's active
        # we do this manually in the workercoordinator since it's fully responsible for the feedback actor
        if hasattr(self, "feedback_actor"):
            self.logger.info("Shutting down FeedbackActor due to benchmark cancellation.")
            self.send(self.feedback_actor, thespian.actors.ActorExitRequest())
        self.send(self.start_sender, msg)

    def receiveMsg_ActorExitRequest(self, msg, sender):
        self.logger.info("Main worker_coordinator received ActorExitRequest and will terminate all load generators.")
        self.status = "exiting"

    def receiveMsg_ChildActorExited(self, msg, sender):
        # is it a worker?
        if msg.childAddress in self.coordinator.workers:
            worker_index = self.coordinator.workers.index(msg.childAddress)
            if self.status == "exiting":
                self.logger.info("Worker [%d] has exited.", worker_index)
            else:
                self.logger.error("Worker [%d] has exited prematurely. Aborting benchmark.", worker_index)
                self.send(self.start_sender, actor.BenchmarkFailure("Worker [{}] has exited prematurely.".format(worker_index)))
        else:
            self.logger.info("A workload preparator has exited.")

    def receiveUnrecognizedMessage(self, msg, sender):
        self.logger.info("Main worker_coordinator received unknown message [%s] (ignoring).", str(msg))

    @actor.no_retry("worker_coordinator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_PrepareBenchmark(self, msg, sender):
        self.start_sender = sender
        self.coordinator = WorkerCoordinator(self, msg.config)
        self.coordinator.prepare_benchmark(msg.workload)

    @actor.no_retry("worker_coordinator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_StartBenchmark(self, msg, sender):
        self.start_sender = sender
        self.coordinator.start_benchmark()
        self.wakeupAfter(datetime.timedelta(seconds=WorkerCoordinatorActor.WAKEUP_INTERVAL_SECONDS))

    @actor.no_retry("worker_coordinator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_WorkloadPrepared(self, msg, sender):
        self.transition_when_all_children_responded(sender, msg,
                                                    expected_status=None, new_status=None, transition=self._after_workload_prepared)

    @actor.no_retry("worker_coordinator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_JoinPointReached(self, msg, sender):
        self.coordinator.joinpoint_reached(msg.worker_id, msg.worker_timestamp, msg.task)

    @actor.no_retry("worker_coordinator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_UpdateSamples(self, msg, sender):
        self.coordinator.update_samples(msg.samples)
        self.coordinator.update_profile_samples(msg.profile_samples)

    @actor.no_retry("worker_coordinator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_WakeupMessage(self, msg, sender):
        if msg.payload == WorkerCoordinatorActor.RESET_RELATIVE_TIME_MARKER:
            self.coordinator.reset_relative_time()
        elif not self.coordinator.finished():
            self.post_process_timer += WorkerCoordinatorActor.WAKEUP_INTERVAL_SECONDS
            if self.post_process_timer >= WorkerCoordinatorActor.POST_PROCESS_INTERVAL_SECONDS:
                self.post_process_timer = 0
                self.coordinator.post_process_samples()
            self.coordinator.update_progress_message()
            self.wakeupAfter(datetime.timedelta(seconds=WorkerCoordinatorActor.WAKEUP_INTERVAL_SECONDS))

    def create_client(self, host):
        return self.createActor(Worker, targetActorRequirements=self._requirements(host))

    def start_worker(self, worker_coordinator, worker_id, cfg, workload, allocations, error_queue=None, queue_lock=None, shared_states=None):
        self.send(worker_coordinator, StartWorker(worker_id, cfg, workload, allocations, self.feedback_actor, error_queue, queue_lock, shared_states))

    def start_feedbackActor(self, shared_states):
        self.send(
            self.feedback_actor,
            StartFeedbackActor(
                shared_states=shared_states,
                error_queue=self.coordinator.error_queue,
                queue_lock=self.coordinator.queue_lock
                )
            )

    def drive_at(self, worker_coordinator, client_start_timestamp):
        self.send(worker_coordinator, Drive(client_start_timestamp))

    def complete_current_task(self, worker_coordinator):
        self.send(worker_coordinator, CompleteCurrentTask())

    def on_task_finished(self, metrics, next_task_scheduled_in):
        if next_task_scheduled_in > 0:
            self.wakeupAfter(datetime.timedelta(seconds=next_task_scheduled_in), payload=WorkerCoordinatorActor.RESET_RELATIVE_TIME_MARKER)
        else:
            self.coordinator.reset_relative_time()
        self.send(self.start_sender, TaskFinished(metrics, next_task_scheduled_in))

    def _requirements(self, host):
        if host == "localhost":
            return {"coordinator": True}
        else:
            return {"ip": host}

    def on_cluster_details_retrieved(self, cluster_details):
        self.cluster_details = cluster_details

    def prepare_workload(self, hosts, cfg, workload):
        self.logger.info("Starting prepare workload process on hosts [%s]", hosts)
        self.children = [self._create_workload_preparator(h) for h in hosts]
        msg = PrepareWorkload(cfg, workload)
        for child in self.children:
            self.send(child, msg)

    def _create_workload_preparator(self, host):
        return self.createActor(WorkloadPreparationActor, targetActorRequirements=self._requirements(host))

    def _after_workload_prepared(self):
        cluster_version = self.cluster_details["version"] if self.cluster_details else {}
        for child in self.children:
            self.send(child, thespian.actors.ActorExitRequest())
        self.children = []
        self.send(self.start_sender, PreparationComplete(
            # older versions (pre 6.3.0) don't expose build_flavor because the only (implicit) flavor was "oss"
            cluster_version.get("build_flavor", "oss"),
            cluster_version.get("number"),
            cluster_version.get("build_hash")
        ))

    def on_benchmark_complete(self, metrics):
        self.send(self.start_sender, BenchmarkComplete(metrics))


def load_local_config(coordinator_config):
    cfg = config.auto_load_local_config(coordinator_config, additional_sections=[
        # only copy the relevant bits
        "workload", "worker_coordinator", "client",
        # due to distribution version...
        "builder",
        "telemetry"
    ])
    # set root path (normally done by the main entry point)
    cfg.add(config.Scope.application, "node", "benchmark.root", paths.benchmark_root())
    return cfg


class TaskExecutionActor(actor.BenchmarkActor):
    """
    This class should be used for long-running tasks, as it ensures they do not block the actor's messaging system
    """
    def __init__(self):
        super().__init__()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.executor_future = None
        self.wakeup_interval = 5
        self.parent = None
        self.logger = logging.getLogger(__name__)
        self.workload_name = None
        self.cfg = None

    @actor.no_retry("task executor")  # pylint: disable=no-value-for-parameter
    def receiveMsg_StartTaskLoop(self, msg, sender):
        self.parent = sender
        self.workload_name = msg.workload_name
        self.cfg = load_local_config(msg.cfg)
        if self.cfg.opts("workload", "test.mode.enabled"):
            self.wakeup_interval = 0.5
        workload.load_workload_plugins(self.cfg, self.workload_name)
        self.send(self.parent, ReadyForWork())

    @actor.no_retry("task executor")  # pylint: disable=no-value-for-parameter
    def receiveMsg_DoTask(self, msg, sender):
        # actor can arbitrarily execute code based on these messages. if anyone besides our parent sends a task, ignore
        if sender != self.parent:
            msg = f"TaskExecutionActor expected message from [{self.parent}] but the received the following from " \
                  f"[{sender}]: {vars(msg)}"
            raise exceptions.BenchmarkError(msg)
        task = msg.task
        if self.executor_future is not None:
            msg = f"TaskExecutionActor received DoTask message [{vars(msg)}], but was already busy"
            raise exceptions.BenchmarkError(msg)
        if task is None:
            self.send(self.parent, WorkerIdle())
        else:
            # this is a potentially long-running operation so we offload it a background thread so we don't block
            # the actor (e.g. logging works properly as log messages are forwarded timely).
            self.executor_future = self.pool.submit(task.func, **task.params)
            self.wakeupAfter(datetime.timedelta(seconds=self.wakeup_interval))

    @actor.no_retry("task executor")  # pylint: disable=no-value-for-parameter
    def receiveMsg_WakeupMessage(self, msg, sender):
        if self.executor_future is not None and self.executor_future.done():
            e = self.executor_future.exception(timeout=0)
            if e:
                self.logger.exception("Worker failed. Notifying parent...", exc_info=e)
                # the exception might be user-defined and not be on the load path of the original sender. Hence, it
                # cannot be deserialized on the receiver so we convert it here to a plain string.
                self.send(self.parent, actor.BenchmarkFailure("Error in task executor", str(e)))
            else:
                self.executor_future = None
                self.send(self.parent, ReadyForWork())
        else:
            self.wakeupAfter(datetime.timedelta(seconds=self.wakeup_interval))

    def receiveMsg_BenchmarkFailure(self, msg, sender):
        # sent by our no_retry infrastructure; forward to master
        self.send(self.parent, msg)

class WorkloadPreparationActor(actor.BenchmarkActor):
    class Status(Enum):
        INITIALIZING = "initializing"
        PROCESSOR_RUNNING = "processor running"
        PROCESSOR_COMPLETE = "processor complete"

    def __init__(self):
        super().__init__()
        self.processors = queue.Queue()
        self.original_sender = None
        self.logger.info("Workload Preparator started")
        self.status = self.Status.INITIALIZING
        self.children = []
        self.tasks = []
        self.cfg = None
        self.data_root_dir = None
        self.workload = None

    def receiveMsg_PoisonMessage(self, poisonmsg, sender):
        self.logger.error("Workload Preparator received a fatal indication from a load generator (%s). Shutting down.", poisonmsg.details)
        self.send(self.original_sender, actor.BenchmarkFailure("Fatal workload preparation indication", poisonmsg.details))

    @actor.no_retry("workload preparator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_ActorExitRequest(self, msg, sender):
        self.logger.info("ActorExitRequest received. Forwarding to children")
        for child in self.children:
            self.send(child, msg)

    @actor.no_retry("workload preparator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_BenchmarkFailure(self, msg, sender):
        # sent by our generic worker; forward to parent
        self.send(self.original_sender, msg)

    @actor.no_retry("workload preparator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_PrepareWorkload(self, msg, sender):
        self.original_sender = sender
        # load node-specific config to have correct paths available
        self.cfg = load_local_config(msg.config)
        self.data_root_dir = self.cfg.opts("benchmarks", "local.dataset.cache")
        tpr = WorkloadProcessorRegistry(self.cfg)
        self.workload = msg.workload
        self.logger.info("Preparing workload [%s]", self.workload.name)
        self.logger.info("Reloading workload [%s] to ensure plugins are up-to-date.", self.workload.name)
        # the workload might have been loaded on a different machine (the coordinator machine) so we force a workload
        # update to ensure we use the latest version of plugins.
        load_workload(self.cfg)
        load_workload_plugins(self.cfg, self.workload.name, register_workload_processor=tpr.register_workload_processor,
                           force_update=True)
        # we expect on_prepare_workload can take a long time. seed a queue of tasks and delegate to child workers
        self.children = [self._create_task_executor() for _ in range(num_cores(self.cfg))]
        for processor in tpr.processors:
            self.processors.put(processor)
        self._seed_tasks(self.processors.get())
        self.send_to_children_and_transition(self, StartTaskLoop(self.workload.name, self.cfg), self.Status.INITIALIZING,
                                             self.Status.PROCESSOR_RUNNING)

    def resume(self):
        if not self.processors.empty():
            self._seed_tasks(self.processors.get())
            self.send_to_children_and_transition(self, StartTaskLoop(self.workload.name, self.cfg), self.Status.PROCESSOR_COMPLETE,
                                                 self.Status.PROCESSOR_RUNNING)
        else:
            self.send(self.original_sender, WorkloadPrepared())

    def _seed_tasks(self, processor):
        self.tasks = list(WorkerTask(func, params) for func, params in
                          processor.on_prepare_workload(self.workload, self.data_root_dir))

    def _create_task_executor(self):
        return self.createActor(TaskExecutionActor)

    @actor.no_retry("workload preparator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_ReadyForWork(self, msg, sender):
        if self.tasks:
            next_task = self.tasks.pop()
        else:
            next_task = None
        new_msg = DoTask(next_task, self.cfg)
        self.logger.debug("Workload Preparator sending %s to %s", vars(new_msg), sender)
        self.send(sender, new_msg)

    @actor.no_retry("workload preparator")  # pylint: disable=no-value-for-parameter
    def receiveMsg_WorkerIdle(self, msg, sender):
        self.transition_when_all_children_responded(sender, msg, self.Status.PROCESSOR_RUNNING,
                                                    self.Status.PROCESSOR_COMPLETE, self.resume)


def num_cores(cfg):
    return int(cfg.opts("system", "available.cores", mandatory=False,
                         default_value=multiprocessing.cpu_count()))


class WorkerCoordinator:
    def __init__(self, target, config, os_client_factory_class=client.OsClientFactory):
        """
        Coordinates all workers. It is technology-agnostic, i.e. it does not know anything about actors. To allow us to hook in an actor,
        we provide a ``target`` parameter which will be called whenever some event has occurred. The ``target`` can use this to send
        appropriate messages.

        :param target: A target that will be notified of important events.
        :param config: The current config object.
        """
        self.logger = logging.getLogger(__name__)
        self.target = target
        self.config = config
        self.os_client_factory = os_client_factory_class
        self.workload = None
        self.test_procedure = None
        self.metrics_store = None
        self.load_worker_coordinator_hosts = []
        self.workers = []
        # which client ids are assigned to which workers?
        self.clients_per_worker = {}
        self.manager = multiprocessing.Manager()
        self.shared_client_dict = self.manager.dict()
        self.error_queue = None
        self.queue_lock = self.manager.Lock()

        self.progress_results_publisher = console.progress()
        self.progress_counter = 0
        self.quiet = False
        self.allocations = None
        self.raw_samples = []
        self.raw_profile_samples = []
        self.most_recent_sample_per_client = {}
        self.sample_post_processor = None
        self.profile_metrics_post_processor = None

        self.number_of_steps = 0
        self.currently_completed = 0
        self.workers_completed_current_step = {}
        self.current_step = -1
        self.tasks_per_join_point = None
        self.complete_current_task_sent = False

        self.telemetry = None

    def create_os_clients(self):
        all_hosts = self.config.opts("client", "hosts").all_hosts
        opensearch = {}
        for cluster_name, cluster_hosts in all_hosts.items():
            all_client_options = self.config.opts("client", "options").all_client_options
            cluster_client_options = dict(all_client_options[cluster_name])
            # Use retries to avoid aborts on long living connections for telemetry devices
            cluster_client_options["retry-on-timeout"] = True
            opensearch[cluster_name] = self.os_client_factory(cluster_hosts, cluster_client_options).create()
        return opensearch

    def prepare_telemetry(self, opensearch, enable):
        enabled_devices = self.config.opts("telemetry", "devices")
        telemetry_params = self.config.opts("telemetry", "params")
        log_root = paths.test_execution_root(self.config)

        os_default = opensearch["default"]

        if enable:
            devices = [
                telemetry.NodeStats(telemetry_params, opensearch, self.metrics_store),
                telemetry.ExternalEnvironmentInfo(os_default, self.metrics_store),
                telemetry.ClusterEnvironmentInfo(os_default, self.metrics_store),
                telemetry.JvmStatsSummary(os_default, self.metrics_store),
                telemetry.IndexStats(os_default, self.metrics_store),
                telemetry.MlBucketProcessingTime(os_default, self.metrics_store),
                telemetry.SegmentStats(log_root, os_default),
                telemetry.CcrStats(telemetry_params, opensearch, self.metrics_store),
                telemetry.RecoveryStats(telemetry_params, opensearch, self.metrics_store),
                telemetry.TransformStats(telemetry_params, opensearch, self.metrics_store),
                telemetry.SearchableSnapshotsStats(telemetry_params, opensearch, self.metrics_store),
                telemetry.SegmentReplicationStats(telemetry_params, opensearch, self.metrics_store),
                telemetry.ShardStats(telemetry_params, opensearch, self.metrics_store)
            ]
        else:
            devices = []
        self.telemetry = telemetry.Telemetry(enabled_devices, devices=devices)

    def wait_for_rest_api(self, opensearch):
        os_default = opensearch["default"]
        self.logger.info("Checking if REST API is available.")
        if client.wait_for_rest_layer(os_default, max_attempts=40):
            self.logger.info("REST API is available.")
        else:
            self.logger.error("REST API layer is not yet available. Stopping benchmark.")
            raise exceptions.SystemSetupError("OpenSearch REST API layer is not available.")

    def retrieve_cluster_info(self, opensearch):
        try:
            return opensearch["default"].info()
        except BaseException:
            self.logger.exception("Could not retrieve cluster info on benchmark start")
            return None

    def prepare_benchmark(self, t):
        self.workload = t
        self.test_procedure = select_test_procedure(self.config, self.workload)
        self.quiet = self.config.opts("system", "quiet.mode", mandatory=False, default_value=False)
        downsample_factor = int(self.config.opts(
            "results_publishing", "metrics.request.downsample.factor",
            mandatory=False, default_value=1))
        self.metrics_store = metrics.metrics_store(cfg=self.config,
                                                   workload=self.workload.name,
                                                   test_procedure=self.test_procedure.name,
                                                   read_only=False)

        self.sample_post_processor = DefaultSamplePostprocessor(self.metrics_store,
                                                         downsample_factor,
                                                         self.workload.meta_data,
                                                         self.test_procedure.meta_data)

        os_clients = self.create_os_clients()

        skip_rest_api_check = self.config.opts("builder", "skip.rest.api.check")
        uses_static_responses = self.config.opts("client", "options").uses_static_responses
        if skip_rest_api_check:
            self.logger.info("Skipping REST API check as requested explicitly.")
        elif uses_static_responses:
            self.logger.info("Skipping REST API check as static responses are used.")
        else:
            self.wait_for_rest_api(os_clients)
            self.target.on_cluster_details_retrieved(self.retrieve_cluster_info(os_clients))

        # Redline testing: Check if cpu feedback is enabled. Enable the node-stats telemetry device if we need to
        cpu_max = self.config.opts("workload", "redline.max_cpu_usage", default_value=None, mandatory=False)
        if cpu_max:
            devices = self.config.opts("telemetry", "devices", default_value=[])
            if "node-stats" not in devices:
                # if node stats aren't enabled but cpu feedback is, add the node-stats telemetry device
                self.logger.info("Enabling node stats telemetry device for CPU-based redline testing.")
                devices = self.config.opts("telemetry", "devices", default_value=[])
                devices.append("node-stats")
                self.config.add(config.Scope.application, "telemetry", "devices", devices)

        # Avoid issuing any requests to the target cluster when static responses are enabled. The results
        # are not useful and attempts to connect to a non-existing cluster just lead to exception traces in logs.
        self.prepare_telemetry(os_clients, enable=not uses_static_responses)

        for host in self.config.opts("worker_coordinator", "load_worker_coordinator_hosts"):
            host_config = {
                # for simplicity we assume that all benchmark machines have the same specs
                "cores": num_cores(self.config)
            }
            if host != "localhost":
                host_config["host"] = net.resolve(host)
            else:
                host_config["host"] = host

            self.load_worker_coordinator_hosts.append(host_config)

        self.target.prepare_workload([h["host"] for h in self.load_worker_coordinator_hosts], self.config, self.workload)

    def start_benchmark(self):
        self.logger.info("OSB is about to start.")
        # ensure relative time starts when the benchmark starts.
        self.reset_relative_time()
        self.logger.info("Attaching cluster-level telemetry devices.")
        self.telemetry.on_benchmark_start()
        self.logger.info("Cluster-level telemetry devices are now attached.")
        # if redline testing or load testing is enabled, modify the client + throughput number for the task(s)
        # target throughput + clients will then be equal to the qps passed in through --redline-test or --load-test
        redline_enabled = self.config.opts("workload", "redline.test", mandatory=False, default_value=False)
        load_test_clients = self.config.opts("workload", "load.test.clients", mandatory=False)
        if redline_enabled:
            max_clients = self.config.opts("workload", "redline.max_clients", mandatory=False, default_value=None)
            self.target.feedback_actor = self.target.createActor(FeedbackActor)
            self.error_queue = self.manager.Queue(maxsize=1000)
            self.logger.info("Redline test mode enabled. Clients will be managed dynamically per task")
            if max_clients is None:
                max_clients = self.test_procedure.schedule[0].clients
            else:
                for task in self.test_procedure.schedule:
                    for subtask in task:
                        subtask.params["target-throughput"] = max_clients
                        subtask.clients = max_clients
        elif load_test_clients:
            for task in self.test_procedure.schedule:
                for subtask in task:
                    subtask.clients = load_test_clients
                    subtask.params["target-throughput"] = load_test_clients
            self.logger.info("Load test mode enabled - set max client count to %d", load_test_clients)
        allocator = Allocator(self.test_procedure.schedule)
        self.allocations = allocator.allocations
        self.number_of_steps = len(allocator.join_points) - 1
        self.tasks_per_join_point = allocator.tasks_per_joinpoint

        self.logger.info("OSB consists of [%d] steps executed by [%d] clients.",
                         self.number_of_steps, len(self.allocations))
        # avoid flooding the log if there are too many clients
        if allocator.clients < 128:
            self.logger.info("Allocation matrix:\n%s", "\n".join([str(a) for a in self.allocations]))

        worker_assignments = calculate_worker_assignments(self.load_worker_coordinator_hosts, allocator.clients)
        worker_id = 0
        # redline testing: keep track of the total number of workers
        # and report this to the feedbackActor before starting a redline test
        for assignment in worker_assignments:
            host = assignment["host"]
            for clients in assignment["workers"]:
                # don't assign workers without any clients
                if len(clients) > 0:
                    self.logger.info("Allocating worker [%d] on [%s] with [%d] clients.", worker_id, host, len(clients))
                    worker = self.target.create_client(host)

                    client_allocations = ClientAllocations()
                    for client_id in clients:
                        client_allocations.add(client_id, self.allocations[client_id])
                        self.clients_per_worker[client_id] = worker_id
                    # if load testing is enabled, create a shared state dictionary for this worker
                    if redline_enabled or load_test_clients:
                        self.shared_client_dict[worker_id] = self.manager.dict()
                        for client_id in clients:
                            self.shared_client_dict[worker_id][client_id] = False
                        # and send it along with the start_worker message. This way, the worker can pass it down to its assigned clients
                        self.target.start_worker(worker, worker_id, self.config, self.workload, client_allocations,
                                                 self.error_queue, self.queue_lock, shared_states=self.shared_client_dict[worker_id])
                    else:
                        self.target.start_worker(worker, worker_id, self.config, self.workload, client_allocations)
                    self.workers.append(worker)
                    worker_id += 1
        if redline_enabled:
            metrics_index = None
            test_execution_id = None
            # we must have a metrics store connected for CPU based feedback
            cpu_max = self.config.opts("workload", "redline.max_cpu_usage", default_value=None, mandatory=False)
            if cpu_max and isinstance(self.metrics_store, metrics.InMemoryMetricsStore):
                raise exceptions.SystemSetupError("CPU-based feedback requires a metrics store. You are using an in-memory metrics store")
            elif cpu_max and "node-stats" not in self.config.opts("telemetry", "devices"):
                raise exceptions.SystemSetupError("Node stats telemetry not enabled — this is required for CPU-based redline feedback.")
            elif cpu_max and isinstance(self.metrics_store, metrics.OsMetricsStore):
                # pass over the index and test execution ID so the feedbackActor can query the datastore
                metrics_index = self.metrics_store.index
                test_execution_id = self.metrics_store.test_execution_id

            scale_step = self.config.opts("workload", "redline.scale_step", default_value=0)
            scale_down_pct = self.config.opts("workload", "redline.scale_down_pct", default_value=0)
            sleep_seconds = self.config.opts("workload", "redline.sleep_seconds", default_value=0)
            cpu_window_seconds = self.config.opts("workload", "redline.cpu_window_seconds", default_value=0)
            cpu_check_interval = self.config.opts("workload", "redline.cpu_check_interval", default_value=0)

            self.target.send(self.target.feedback_actor, ConfigureFeedbackScaling(
            scale_step=scale_step,
            scale_down_pct=scale_down_pct,
            sleep_seconds=sleep_seconds,
            max_clients=max_clients,
            cpu_max=cpu_max,
            cpu_window_seconds=cpu_window_seconds,
            cpu_check_interval=cpu_check_interval,
            cfg=self.config,
            metrics_index=metrics_index,
            test_execution_id=test_execution_id
            ))
            self.target.start_feedbackActor(self.shared_client_dict)

        self.update_progress_message()

    def joinpoint_reached(self, worker_id, worker_local_timestamp, task_allocations):
        self.currently_completed += 1
        self.workers_completed_current_step[worker_id] = (worker_local_timestamp, time.perf_counter())
        self.logger.info("[%d/%d] workers reached join point [%d/%d].",
                         self.currently_completed, len(self.workers), self.current_step + 1, self.number_of_steps)
        # if we're in redline test mode, disable the feedback actor and pause all clients when we're at a joinpoint
        if self.config.opts("workload", "redline.test", mandatory=False):
            self.target.send(self.target.feedback_actor, DisableFeedbackScaling())
        if self.currently_completed == len(self.workers):
            self.logger.info("All workers completed their tasks until join point [%d/%d].", self.current_step + 1, self.number_of_steps)
            # we can go on to the next step
            self.currently_completed = 0
            self.complete_current_task_sent = False
            # make a copy and reset early to avoid any test
            # execution conditions from clients that reach a
            # join point already while we are sending...
            workers_curr_step = self.workers_completed_current_step
            self.workers_completed_current_step = {}
            self.update_progress_message(task_finished=True)
            # clear per step
            self.most_recent_sample_per_client = {}
            self.current_step += 1

            self.logger.debug("Postprocessing samples...")
            self.post_process_samples()
            if self.finished():
                self.telemetry.on_benchmark_stop()
                self.logger.info("All steps completed.")
                # Some metrics store implementations return None because no external representation is required.
                # pylint: disable=assignment-from-none
                m = self.metrics_store.to_externalizable(clear=True)
                self.logger.debug("Closing metrics store...")
                self.metrics_store.close()
                # immediately clear as we don't need it anymore and it can consume a significant amount of memory
                self.metrics_store = None
                self.logger.debug("Sending benchmark results...")
                self.target.on_benchmark_complete(m)
            else:
                self.move_to_next_task(workers_curr_step)
                # re-enable the feedback actor for the next task if we're in redline testing
                if self.config.opts("workload", "redline.test", mandatory=False):
                    self.target.send(self.target.feedback_actor, EnableFeedbackScaling())
        else:
            self.may_complete_current_task(task_allocations)

    def move_to_next_task(self, workers_curr_step):
        if self.config.opts("workload", "test.mode.enabled"):
            # don't wait if test mode is enabled and start the next task immediately.
            waiting_period = 0
        else:
            # start the next task in one second (relative to master's timestamp)
            #
            # Assumption: We don't have a lot of clock skew between reaching the join point and sending the next task
            #             (it doesn't matter too much if we're a few ms off).
            waiting_period = 1.0
        # Some metrics store implementations return None because no external representation is required.
        # pylint: disable=assignment-from-none
        m = self.metrics_store.to_externalizable(clear=True)
        self.target.on_task_finished(m, waiting_period)
        # Using a perf_counter here is fine also in the distributed case as we subtract it from `master_received_msg_at` making it
        # a relative instead of an absolute value.
        start_next_task = time.perf_counter() + waiting_period
        for worker_id, worker in enumerate(self.workers):
            worker_ended_task_at, master_received_msg_at = workers_curr_step[worker_id]
            worker_start_timestamp = worker_ended_task_at + (start_next_task - master_received_msg_at)
            self.logger.info("Scheduling next task for worker id [%d] at their timestamp [%f] (master timestamp [%f])",
                             worker_id, worker_start_timestamp, start_next_task)
            self.target.drive_at(worker, worker_start_timestamp)

    def may_complete_current_task(self, task_allocations):
        joinpoints_completing_parent = [a for a in task_allocations if a.task.preceding_task_completes_parent]
        # we need to actively send CompleteCurrentTask messages to all remaining workers.
        if len(joinpoints_completing_parent) > 0 and not self.complete_current_task_sent:
            # while this list could contain multiple items, it should always be the same task (but multiple
            # different clients) so any item is sufficient.
            current_join_point = joinpoints_completing_parent[0].task
            self.logger.info("Tasks before join point [%s] are able to complete the parent structure. Checking "
                             "if all [%d] clients have finished yet.",
                             current_join_point, len(current_join_point.clients_executing_completing_task))

            pending_client_ids = []
            for client_id in current_join_point.clients_executing_completing_task:
                # We assume that all clients have finished if their corresponding worker has finished
                worker_id = self.clients_per_worker[client_id]
                if worker_id not in self.workers_completed_current_step:
                    pending_client_ids.append(client_id)

            # are all clients executing said task already done? if so we need to notify the remaining clients
            if len(pending_client_ids) == 0:
                # As we are waiting for other clients to finish, we would send this message over and over again.
                # Hence we need to memorize whether we have already sent it for the current step.
                self.complete_current_task_sent = True
                self.logger.info("All affected clients have finished. Notifying all clients to complete their current tasks.")
                for worker in self.workers:
                    self.target.complete_current_task(worker)
            else:
                if len(pending_client_ids) > 32:
                    self.logger.info("[%d] clients did not yet finish.", len(pending_client_ids))
                else:
                    self.logger.info("Client id(s) [%s] did not yet finish.", ",".join(map(str, pending_client_ids)))

    def reset_relative_time(self):
        self.logger.debug("Resetting relative time of request metrics store.")
        self.metrics_store.reset_relative_time()

    def finished(self):
        return self.current_step == self.number_of_steps

    def close(self):
        self.progress_results_publisher.finish()
        if self.metrics_store and self.metrics_store.opened:
            self.metrics_store.close()

    def update_samples(self, samples):
        if len(samples) > 0:
            self.raw_samples += samples
            # We need to check all samples, they will be from different clients
            for s in samples:
                self.most_recent_sample_per_client[s.client_id] = s

    def update_profile_samples(self, profile_samples):
        if len(profile_samples) > 0:
            self.raw_profile_samples += profile_samples

    def update_progress_message(self, task_finished=False):
        if not self.quiet and self.current_step >= 0:
            tasks = ",".join([t.name for t in self.tasks_per_join_point[self.current_step]])

            if task_finished:
                total_progress = 1.0
            else:
                # we only count clients which actually contribute to progress. If clients are executing tasks eternally in a parallel
                # structure, we should not count them. The reason is that progress depends entirely on the client(s) that execute the
                # task that is completing the parallel structure.
                progress_per_client = [s.percent_completed
                                       for s in self.most_recent_sample_per_client.values() if s.percent_completed is not None]

                num_clients = max(len(progress_per_client), 1)
                total_progress = sum(progress_per_client) / num_clients
            self.progress_results_publisher.print("Running %s" % tasks, "[%3d%% done]" % (round(total_progress * 100)))
            if task_finished:
                self.progress_results_publisher.finish()

    def post_process_samples(self):
        # we do *not* do this here to avoid concurrent updates (actors are single-threaded) but rather to make it clear that we use
        # only a snapshot and that new data will go to a new sample set.
        raw_samples = self.raw_samples
        self.raw_samples = []
        self.sample_post_processor(raw_samples)
        profile_samples = self.raw_profile_samples
        self.raw_profile_samples = []
        if len(profile_samples) > 0:
            if self.profile_metrics_post_processor is None:
                self.profile_metrics_post_processor = ProfileMetricsSamplePostprocessor(self.metrics_store,
                                                                                    self.workload.meta_data,
                                                                                    self.test_procedure.meta_data)
            self.profile_metrics_post_processor(profile_samples)

class SamplePostprocessor():
    """
    Parent class used to process samples into the metrics store
    """
    def __init__(self, metrics_store, workload_meta_data, test_procedure_meta_data):
        self.logger = logging.getLogger(__name__)
        self.metrics_store = metrics_store
        self.workload_meta_data = workload_meta_data
        self.test_procedure_meta_data = test_procedure_meta_data

    def merge(self, *args):
        result = {}
        for arg in args:
            if arg is not None:
                result.update(arg)
        return result


class DefaultSamplePostprocessor(SamplePostprocessor):
    """
    Processes operational and correctness metric samples by merging and adding to the metrics store
    """
    def __init__(self, metrics_store, downsample_factor, workload_meta_data, test_procedure_meta_data):
        super().__init__(metrics_store, workload_meta_data, test_procedure_meta_data)
        self.throughput_calculator = ThroughputCalculator()
        self.downsample_factor = downsample_factor

    def __call__(self, raw_samples):
        if len(raw_samples) == 0:
            return
        total_start = time.perf_counter()
        start = total_start
        final_sample_count = 0
        for idx, sample in enumerate(raw_samples):
            self.logger.debug(
                "All sample meta data: [%s],[%s],[%s],[%s],[%s]",
                self.workload_meta_data,
                self.test_procedure_meta_data,
                sample.operation_meta_data,
                sample.task.meta_data,
                sample.request_meta_data,
            )

            # if request_meta_data exists then it will have {"success": true/false} as a parameter.
            if sample.request_meta_data and len(sample.request_meta_data) > 1:
                self.logger.debug("Found: %s", sample.request_meta_data)

                recall_metric_names = ["recall@k", "recall@1"]

                for recall_metric_name in recall_metric_names:
                    if recall_metric_name in sample.request_meta_data:
                        meta_data = self.merge(
                            self.workload_meta_data,
                            self.test_procedure_meta_data,
                            sample.operation_meta_data,
                            sample.task.meta_data,
                            sample.request_meta_data,
                        )

                        self.metrics_store.put_value_cluster_level(
                            name=recall_metric_name,
                            value=sample.request_meta_data[recall_metric_name],
                            unit="",
                            task=sample.task.name,
                            operation=sample.operation_name,
                            operation_type=sample.operation_type,
                            sample_type=sample.sample_type,
                            absolute_time=sample.absolute_time,
                            relative_time=sample.relative_time,
                            meta_data=meta_data,
                        )

            if idx % self.downsample_factor == 0:
                final_sample_count += 1
                meta_data = self.merge(
                    self.workload_meta_data,
                    self.test_procedure_meta_data,
                    sample.operation_meta_data,
                    sample.task.meta_data,
                    sample.request_meta_data)

                self.metrics_store.put_value_cluster_level(name="latency", value=convert.seconds_to_ms(sample.latency),
                                                           unit="ms", task=sample.task.name,
                                                           operation=sample.operation_name, operation_type=sample.operation_type,
                                                           sample_type=sample.sample_type, absolute_time=sample.absolute_time,
                                                           relative_time=sample.relative_time, meta_data=meta_data)

                self.metrics_store.put_value_cluster_level(name="service_time", value=convert.seconds_to_ms(sample.service_time),
                                                           unit="ms", task=sample.task.name,
                                                           operation=sample.operation_name, operation_type=sample.operation_type,
                                                           sample_type=sample.sample_type, absolute_time=sample.absolute_time,
                                                           relative_time=sample.relative_time, meta_data=meta_data)

                self.metrics_store.put_value_cluster_level(name="client_processing_time",
                                                           value=convert.seconds_to_ms(sample.client_processing_time),
                                                           unit="ms", task=sample.task.name,
                                                           operation=sample.operation_name, operation_type=sample.operation_type,
                                                           sample_type=sample.sample_type, absolute_time=sample.absolute_time,
                                                           relative_time=sample.relative_time, meta_data=meta_data)

                self.metrics_store.put_value_cluster_level(name="processing_time", value=convert.seconds_to_ms(sample.processing_time),
                                                           unit="ms", task=sample.task.name,
                                                           operation=sample.operation_name, operation_type=sample.operation_type,
                                                           sample_type=sample.sample_type, absolute_time=sample.absolute_time,
                                                           relative_time=sample.relative_time, meta_data=meta_data)

                for timing in sample.dependent_timings:
                    self.metrics_store.put_value_cluster_level(name="service_time", value=convert.seconds_to_ms(timing.service_time),
                                                               unit="ms", task=timing.task.name,
                                                               operation=timing.operation_name, operation_type=timing.operation_type,
                                                               sample_type=timing.sample_type, absolute_time=timing.absolute_time,
                                                               relative_time=timing.relative_time, meta_data=meta_data)

        end = time.perf_counter()
        self.logger.debug("Storing latency and service time took [%f] seconds.", (end - start))
        start = end
        aggregates = self.throughput_calculator.calculate(raw_samples)
        end = time.perf_counter()
        self.logger.debug("Calculating throughput took [%f] seconds.", (end - start))
        start = end
        for task, samples in aggregates.items():
            meta_data = self.merge(
                self.workload_meta_data,
                self.test_procedure_meta_data,
                task.operation.meta_data,
                task.meta_data
            )
            for absolute_time, relative_time, sample_type, throughput, throughput_unit in samples:
                self.metrics_store.put_value_cluster_level(name="throughput", value=throughput, unit=throughput_unit, task=task.name,
                                                           operation=task.operation.name, operation_type=task.operation.type,
                                                           sample_type=sample_type, absolute_time=absolute_time,
                                                           relative_time=relative_time, meta_data=meta_data)
        end = time.perf_counter()
        self.logger.debug("Storing throughput took [%f] seconds.", (end - start))
        start = end
        # this will be a noop for the in-memory metrics store. If we use an ES metrics store however, this will ensure that we already send
        # the data and also clear the in-memory buffer. This allows users to see data already while running the benchmark. In cases where
        # it does not matter (i.e. in-memory) we will still defer this step until the end.
        #
        # Don't force refresh here in the interest of short processing times. We don't need to query immediately afterwards so there is
        # no need for frequent refreshes.
        self.metrics_store.flush(refresh=False)
        end = time.perf_counter()
        self.logger.debug("Flushing the metrics store took [%f] seconds.", (end - start))
        self.logger.debug("Postprocessing [%d] raw samples (downsampled to [%d] samples) took [%f] seconds in total.",
                          len(raw_samples), final_sample_count, (end - total_start))


class ProfileMetricsSamplePostprocessor(SamplePostprocessor):
    """
    Processes profile metric samples by merging and adding to the metrics store
    """

    def __call__(self, raw_samples):
        if len(raw_samples) == 0:
            return
        total_start = time.perf_counter()
        start = total_start
        final_sample_count = 0
        for sample in raw_samples:
            final_sample_count += 1
            self.logger.debug(
                "All sample meta data: [%s],[%s],[%s],[%s],[%s]",
                self.workload_meta_data,
                self.test_procedure_meta_data,
                sample.operation_meta_data,
                sample.task.meta_data,
                sample.request_meta_data,
            )

            # if request_meta_data exists then it will have {"success": true/false} as a parameter.
            if sample.request_meta_data and len(sample.request_meta_data) > 1:
                self.logger.debug("Found: %s", sample.request_meta_data)

                if "profile-metrics" in sample.request_meta_data:
                    for metric_name, metric_value in sample.request_meta_data["profile-metrics"].items():
                        meta_data = self.merge(
                            self.workload_meta_data,
                            self.test_procedure_meta_data,
                            sample.operation_meta_data,
                            sample.task.meta_data,
                            sample.request_meta_data,
                        )

                        self.metrics_store.put_value_cluster_level(
                            name=metric_name,
                            value=metric_value,
                            unit="",
                            task=sample.task.name,
                            operation=sample.operation_name,
                            operation_type=sample.operation_type,
                            sample_type=sample.sample_type,
                            absolute_time=sample.absolute_time,
                            relative_time=sample.relative_time,
                            meta_data=meta_data,
                        )

        start = time.perf_counter()
        # this will be a noop for the in-memory metrics store. If we use an ES metrics store however, this will ensure that we already send
        # the data and also clear the in-memory buffer. This allows users to see data already while running the benchmark. In cases where
        # it does not matter (i.e. in-memory) we will still defer this step until the end.
        #
        # Don't force refresh here in the interest of short processing times. We don't need to query immediately afterwards so there is
        # no need for frequent refreshes.
        self.metrics_store.flush(refresh=False)
        end = time.perf_counter()
        self.logger.debug("Flushing the metrics store took [%f] seconds.", (end - start))
        self.logger.debug("Postprocessing [%d] raw samples (downsampled to [%d] samples) took [%f] seconds in total.",
                          len(raw_samples), final_sample_count, (end - total_start))


def calculate_worker_assignments(host_configs, client_count):
    """
    Assigns clients to workers on the provided hosts.

    :param host_configs: A list of dicts where each dict contains the host name (key: ``host``) and the number of
                         available CPU cores (key: ``cores``).
    :param client_count: The number of clients that should be used at most.
    :return: A list of dicts containing the host (key: ``host``) and a list of workers (key ``workers``). Each entry
             in that list contains another list with the clients that should be assigned to these workers.
    """
    assignments = []
    client_idx = 0
    host_count = len(host_configs)
    clients_per_host = math.ceil(client_count / host_count)
    remaining_clients = client_count
    for host_config in host_configs:
        # the last host might not need to simulate as many clients as the rest of the hosts as we eagerly
        # assign clients to hosts.
        clients_on_this_host = min(clients_per_host, remaining_clients)
        assignment = {
            "host": host_config["host"],
            "workers": [],
        }
        assignments.append(assignment)

        workers_on_this_host = host_config["cores"]
        clients_per_worker = [0] * workers_on_this_host

        # determine how many clients each worker should simulate
        for c in range(clients_on_this_host):
            clients_per_worker[c % workers_on_this_host] += 1

        # assign client ids to workers
        for client_count_for_worker in clients_per_worker:
            worker_assignment = []
            assignment["workers"].append(worker_assignment)
            for c in range(client_idx, client_idx + client_count_for_worker):
                worker_assignment.append(c)
            client_idx += client_count_for_worker

        remaining_clients -= clients_on_this_host

    assert remaining_clients == 0

    return assignments


ClientAllocation = collections.namedtuple("ClientAllocation", ["client_id", "task"])


class ClientAllocations:
    def __init__(self):
        self.allocations = []

    def add(self, client_id, tasks):
        self.allocations.append({
            "client_id": client_id,
            "tasks": tasks
        })

    def is_joinpoint(self, task_index):
        return all(isinstance(t.task, JoinPoint) for t in self.tasks(task_index))

    def tasks(self, task_index, remove_empty=True):
        current_tasks = []
        for allocation in self.allocations:
            tasks_at_index = allocation["tasks"][task_index]
            if remove_empty and tasks_at_index is not None:
                current_tasks.append(ClientAllocation(allocation["client_id"], tasks_at_index))
        return current_tasks


class Worker(actor.BenchmarkActor):
    """
    The actual worker that applies load against the cluster(s).

    It will also regularly send measurements to the master node so it can consolidate them.
    """

    WAKEUP_INTERVAL_SECONDS = 5

    def __init__(self):
        super().__init__()
        self.master = None
        self.worker_id = None
        self.config = None
        self.workload = None
        self.client_allocations = None
        self.current_task_index = 0
        self.next_task_index = 0
        self.on_error = None
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # cancellation via future does not work, hence we use our own mechanism with a shared variable and polling
        self.cancel = threading.Event()
        # used to indicate that we want to prematurely consider this completed. This is *not* due to cancellation
        # but a regular event in a benchmark and used to model task dependency of parallel tasks.
        self.complete = threading.Event()
        self.executor_future = None
        self.sampler = None
        self.start_driving = False
        self.wakeup_interval = Worker.WAKEUP_INTERVAL_SECONDS
        self.sample_queue_size = None
        self.shared_states = None
        self.feedback_actor = None
        self.error_queue = None
        self.queue_lock = None

    @actor.no_retry("worker")  # pylint: disable=no-value-for-parameter
    def receiveMsg_StartWorker(self, msg, sender):
        self.logger.info("Worker[%d] is about to start.", msg.worker_id)
        self.master = sender
        self.worker_id = msg.worker_id
        self.config = load_local_config(msg.config)
        self.on_error = self.config.opts("worker_coordinator", "on.error")
        self.sample_queue_size = int(self.config.opts("results_publishing", "sample.queue.size", mandatory=False, default_value=1 << 20))
        self.workload = msg.workload
        workload.set_absolute_data_path(self.config, self.workload)
        self.client_allocations = msg.client_allocations
        self.current_task_index = 0
        self.cancel.clear()
        self.feedback_actor = msg.feedback_actor
        self.shared_states = msg.shared_states
        self.error_queue = msg.error_queue
        self.queue_lock = msg.queue_lock
        # we need to wake up more often in test mode
        if self.config.opts("workload", "test.mode.enabled"):
            self.wakeup_interval = 0.5
        runner.register_default_runners()
        if self.workload.has_plugins:
            workload.load_workload_plugins(self.config, self.workload.name, runner.register_runner, scheduler.register_scheduler)
        self.drive()

    @actor.no_retry("worker")  # pylint: disable=no-value-for-parameter
    def receiveMsg_Drive(self, msg, sender):
        sleep_time = datetime.timedelta(seconds=msg.client_start_timestamp - time.perf_counter())
        self.logger.info("Worker[%d] is continuing its work at task index [%d] on [%f], that is in [%s].",
                         self.worker_id, self.current_task_index, msg.client_start_timestamp, sleep_time)
        self.start_driving = True
        self.wakeupAfter(sleep_time)

    @actor.no_retry("worker")  # pylint: disable=no-value-for-parameter
    def receiveMsg_CompleteCurrentTask(self, msg, sender):
        # finish now ASAP. Remaining samples will be sent with the next WakeupMessage. We will also need to skip to the next
        # JoinPoint. But if we are already at a JoinPoint at the moment, there is nothing to do.
        if self.at_joinpoint():
            self.logger.info("Worker[%s] has received CompleteCurrentTask but is currently at join point at index [%d]. Ignoring.",
                             str(self.worker_id), self.current_task_index)
        else:
            self.logger.info("Worker[%s] has received CompleteCurrentTask. Completing tasks at index [%d].",
                             str(self.worker_id), self.current_task_index)
            self.complete.set()

    @actor.no_retry("worker")  # pylint: disable=no-value-for-parameter
    def receiveMsg_WakeupMessage(self, msg, sender):
        # it would be better if we could send ourselves a message at a specific time, simulate this with a boolean...
        if self.start_driving:
            self.start_driving = False
            self.drive()
        else:
            current_samples = self.send_samples()
            if self.cancel.is_set():
                self.logger.info("Worker[%s] has detected that benchmark has been cancelled. Notifying master...",
                                 str(self.worker_id))
                self.send(self.master, actor.BenchmarkCancelled())
            elif self.executor_future is not None and self.executor_future.done():
                e = self.executor_future.exception(timeout=0)
                if e:
                    currentTasks = self.client_allocations.tasks(self.current_task_index)
                    detailed_error = (
                    f"Benchmark operation failed:\n"
                    f"Worker ID: {self.worker_id}\n"
                    f"Task: {', '.join(t.task.task.name for t in currentTasks)}\n"
                    f"Workload: {self.workload.name if self.workload else 'Unknown'}\n"
                    f"Test Procedure: {self.workload.selected_test_procedure_or_default}\n"
                    f"Cause: {e.cause if hasattr(e, 'cause') and e.cause is not None else 'Unknown'}"
                    )
                    detailed_error += f"\nError: {str(e)}"

                    self.logger.exception(
                        "Worker[%s] has detected a benchmark failure:\n%s",
                        str(self.worker_id),
                        detailed_error,
                        exc_info=e
                    )

                    self.send(
                        self.master,
                        actor.BenchmarkFailure(
                            detailed_error,
                            str(e)
                        )
                    )
                else:
                    self.logger.info("Worker[%s] is ready for the next task.", str(self.worker_id))
                    self.executor_future = None
                    self.drive()
            else:
                if current_samples and len(current_samples) > 0:
                    most_recent_sample = current_samples[-1]
                    if most_recent_sample.percent_completed is not None:
                        self.logger.debug("Worker[%s] is executing [%s] (%.2f%% complete).",
                                          str(self.worker_id), most_recent_sample.task, most_recent_sample.percent_completed * 100.0)
                    else:
                        # TODO: This could be misleading given that one worker could execute more than one task...
                        self.logger.debug("Worker[%s] is executing [%s] (dependent eternal task).",
                                          str(self.worker_id), most_recent_sample.task)
                else:
                    self.logger.debug("Worker[%s] is executing (no samples).", str(self.worker_id))
                self.wakeupAfter(datetime.timedelta(seconds=self.wakeup_interval))

    def receiveMsg_ActorExitRequest(self, msg, sender):
        self.logger.info("Worker[%s] has received ActorExitRequest.", str(self.worker_id))
        if self.executor_future is not None and self.executor_future.running():
            self.cancel.set()
        self.pool.shutdown()
        self.logger.info("Worker[%s] is exiting due to ActorExitRequest.", str(self.worker_id))

    def receiveMsg_BenchmarkFailure(self, msg, sender):
        # sent by our no_retry infrastructure; forward to master
        self.send(self.master, msg)

    def receiveUnrecognizedMessage(self, msg, sender):
        self.logger.info("Worker[%d] received unknown message [%s] (ignoring).", self.worker_id, str(msg))

    def drive(self):
        task_allocations = self.current_tasks_and_advance()
        # skip non-tasks in the task list
        while len(task_allocations) == 0:
            task_allocations = self.current_tasks_and_advance()

        if self.at_joinpoint():
            self.logger.info("Worker[%d] reached join point at index [%d].", self.worker_id, self.current_task_index)
            # clients that don't execute tasks don't need to care about waiting
            if self.executor_future is not None:
                self.executor_future.result()
            self.send_samples()
            self.cancel.clear()
            self.complete.clear()
            self.executor_future = None
            self.sampler = None
            self.send(self.master, JoinPointReached(self.worker_id, task_allocations))
        else:
            # There may be a situation where there are more (parallel) tasks than workers. If we were asked to complete all tasks, we not
            # only need to complete actively running tasks but actually all scheduled tasks until we reach the next join point.
            if self.complete.is_set():
                self.logger.info("Worker[%d] skips tasks at index [%d] because it has been asked to complete all "
                                 "tasks until next join point.", self.worker_id, self.current_task_index)
            else:
                self.logger.info("Worker[%d] is executing tasks at index [%d].", self.worker_id, self.current_task_index)
                self.sampler = DefaultSampler(start_timestamp=time.perf_counter(), buffer_size=self.sample_queue_size)
                self.profile_sampler = ProfileMetricsSampler(start_timestamp=time.perf_counter(), buffer_size=self.sample_queue_size)
                executor = AsyncIoAdapter(self.config, self.workload, task_allocations, self.sampler, self.profile_sampler,
                                          self.cancel, self.complete, self.on_error, self.shared_states, self.feedback_actor, self.error_queue, self.queue_lock)

                self.executor_future = self.pool.submit(executor)
                self.wakeupAfter(datetime.timedelta(seconds=self.wakeup_interval))

    def at_joinpoint(self):
        return self.client_allocations.is_joinpoint(self.current_task_index)

    def current_tasks_and_advance(self):
        self.current_task_index = self.next_task_index
        current = self.client_allocations.tasks(self.current_task_index)
        self.next_task_index += 1
        self.logger.debug("Worker[%d] is at task index [%d].", self.worker_id, self.current_task_index)
        return current

    def send_samples(self):
        if self.sampler:
            samples = self.sampler.samples
            if len(samples) > 0:
                self.send(self.master, UpdateSamples(self.worker_id, samples, self.profile_sampler.samples))
            return samples
        return None


class Sampler:
    """
    Encapsulates management of gathered samples.
    """

    def __init__(self, start_timestamp, buffer_size=16384):
        self.start_timestamp = start_timestamp
        self.q = queue.Queue(maxsize=buffer_size)
        self.logger = logging.getLogger(__name__)

    @property
    def samples(self):
        samples = []
        try:
            while True:
                samples.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return samples

class DefaultSampler(Sampler):
    """
    Encapsulates management of gathered default samples (operational and correctness metrics).
    """

    def add(self, task, client_id, sample_type, meta_data, absolute_time, request_start, latency, service_time,
            client_processing_time, processing_time, throughput, ops, ops_unit, time_period, percent_completed,
            dependent_timing=None):
        try:
            self.q.put_nowait(
                DefaultSample(client_id, absolute_time, request_start, self.start_timestamp, task, sample_type, meta_data,
                       latency, service_time, client_processing_time, processing_time, throughput, ops, ops_unit, time_period,
                       percent_completed, dependent_timing))
        except queue.Full:
            self.logger.warning("Dropping sample for [%s] due to a full sampling queue.", task.operation.name)

class ProfileMetricsSampler(Sampler):
    """
    Encapsulates management of gathered profile metrics samples.
    """

    def add(self, task, client_id, sample_type, meta_data, absolute_time, request_start, time_period, percent_completed,
            dependent_timing=None):
        try:
            self.q.put_nowait(
                ProfileMetricsSample(client_id, absolute_time, request_start, self.start_timestamp, task, sample_type, meta_data,
                       time_period, percent_completed, dependent_timing))
        except queue.Full:
            self.logger.warning("Dropping sample for [%s] due to a full sampling queue.", task.operation.name)


class Sample:
    """
    Basic information used by metrics store to keep track of samples
    """
    def __init__(self, client_id, absolute_time, request_start, task_start, task, sample_type, request_meta_data,
                time_period, percent_completed, dependent_timing=None):
        self.client_id = client_id
        self.absolute_time = absolute_time
        self.request_start = request_start
        self.task_start = task_start
        self.task = task
        self.sample_type = sample_type
        self.request_meta_data = request_meta_data
        self.time_period = time_period
        self._dependent_timing = dependent_timing
        # may be None for eternal tasks!
        self.percent_completed = percent_completed

    @property
    def operation_name(self):
        return self.task.operation.name

    @property
    def operation_type(self):
        return self.task.operation.type

    @property
    def operation_meta_data(self):
        return self.task.operation.meta_data

    @property
    def relative_time(self):
        return self.request_start - self.task_start

    def __repr__(self, *args, **kwargs):
        return f"[{self.absolute_time}; {self.relative_time}] [client [{self.client_id}]] [{self.task}] " \
               f"[{self.sample_type}]"

class DefaultSample(Sample):
    """
    Stores the operational and correctness metrics to later put into the metrics store
    """
    def __init__(self, client_id, absolute_time, request_start, task_start, task, sample_type, request_meta_data, latency,
                 service_time, client_processing_time, processing_time, throughput, total_ops, total_ops_unit, time_period,
                 percent_completed, dependent_timing=None):
        super().__init__(client_id, absolute_time, request_start, task_start, task, sample_type, request_meta_data, time_period, percent_completed, dependent_timing)
        self.latency = latency
        self.service_time = service_time
        self.client_processing_time = client_processing_time
        self.processing_time = processing_time
        self.throughput = throughput
        self.total_ops = total_ops
        self.total_ops_unit = total_ops_unit

    @property
    def dependent_timings(self):
        if self._dependent_timing:
            for t in self._dependent_timing:
                yield DefaultSample(self.client_id, t["absolute_time"], t["request_start"], self.task_start, self.task,
                             self.sample_type, self.request_meta_data, 0, t["service_time"], 0, 0, 0, self.total_ops,
                             self.total_ops_unit, self.time_period, self.percent_completed, None)

    def __repr__(self, *args, **kwargs):
        return f"[{self.absolute_time}; {self.relative_time}] [client [{self.client_id}]] [{self.task}] " \
               f"[{self.sample_type}]: [{self.latency}s] request latency, [{self.service_time}s] service time, " \
               f"[{self.total_ops} {self.total_ops_unit}]"

class ProfileMetricsSample(Sample):
    """
    Stores the profile metrics to later put into the metrics store
    """

    @property
    def dependent_timings(self):
        if self._dependent_timing:
            for t in self._dependent_timing:
                yield ProfileMetricsSample(self.client_id, t["absolute_time"], t["request_start"], self.task_start, self.task,
                             self.sample_type, self.request_meta_data, self.time_period, self.percent_completed, None)


def select_test_procedure(config, t):
    test_procedure_name = config.opts("workload", "test_procedure.name")
    selected_test_procedure = t.find_test_procedure_or_default(test_procedure_name)

    if not selected_test_procedure:
        raise exceptions.SystemSetupError("Unknown test_procedure [%s] for workload [%s]. You can list the available workloads and their "
                                          "test_procedures with %s list workloads." % (test_procedure_name, t.name, PROGRAM_NAME))
    return selected_test_procedure


class ThroughputCalculator:
    class TaskStats:
        """
        Stores per task numbers needed for throughput calculation in between multiple calculations.
        """
        def __init__(self, bucket_interval, sample_type, start_time):
            self.unprocessed = []
            self.total_count = 0
            self.interval = 0
            self.bucket_interval = bucket_interval
            # the first bucket is complete after one bucket interval is over
            self.bucket = bucket_interval
            self.sample_type = sample_type
            self.has_samples_in_sample_type = False
            # start relative to the beginning of our (calculation) time slice.
            self.start_time = start_time

        @property
        def throughput(self):
            return self.total_count / self.interval

        def maybe_update_sample_type(self, current_sample_type):
            if self.sample_type < current_sample_type:
                self.sample_type = current_sample_type
                self.has_samples_in_sample_type = False

        def update_interval(self, absolute_sample_time):
            self.interval = max(absolute_sample_time - self.start_time, self.interval)

        def can_calculate_throughput(self):
            return self.interval > 0 and self.interval >= self.bucket

        def can_add_final_throughput_sample(self):
            return self.interval > 0 and not self.has_samples_in_sample_type

        def finish_bucket(self, new_total):
            self.unprocessed = []
            self.total_count = new_total
            self.has_samples_in_sample_type = True
            self.bucket = int(self.interval) + self.bucket_interval

    def __init__(self):
        self.task_stats = {}

    def calculate(self, samples, bucket_interval_secs=1):
        """
        Calculates global throughput based on samples gathered from multiple load generators.

        :param samples: A list containing all samples from all load generators.
        :param bucket_interval_secs: The bucket interval for aggregations.
        :return: A global view of throughput samples.
        """

        samples_per_task = {}
        # first we group all samples by task (operation).
        for sample in samples:
            k = sample.task
            if k not in samples_per_task:
                samples_per_task[k] = []
            samples_per_task[k].append(sample)

        global_throughput = {}
        # with open("raw_samples_new.csv", "a") as sample_log:
        # print("client_id,absolute_time,relative_time,operation,sample_type,total_ops,time_period", file=sample_log)
        for k, v in samples_per_task.items():
            task = k
            if task not in global_throughput:
                global_throughput[task] = []
            # sort all samples by time
            if task in self.task_stats:
                samples = itertools.chain(v, self.task_stats[task].unprocessed)
            else:
                samples = v
            current_samples = sorted(samples, key=lambda s: s.absolute_time)

            # Calculate throughput based on service time if the runner does not provide one, otherwise use it as is and
            # only transform the values into the expected structure.
            first_sample = current_samples[0]
            if first_sample.throughput is None:
                task_throughput = self.calculate_task_throughput(task, current_samples, bucket_interval_secs)
            else:
                task_throughput = self.map_task_throughput(current_samples)
            global_throughput[task].extend(task_throughput)

        return global_throughput

    def calculate_task_throughput(self, task, current_samples, bucket_interval_secs):
        task_throughput = []

        if task not in self.task_stats:
            first_sample = current_samples[0]
            self.task_stats[task] = ThroughputCalculator.TaskStats(bucket_interval=bucket_interval_secs,
                                                                   sample_type=first_sample.sample_type,
                                                                   start_time=first_sample.absolute_time - first_sample.time_period)
        current = self.task_stats[task]
        count = current.total_count
        last_sample = None
        for sample in current_samples:
            last_sample = sample
            # print("%d,%f,%f,%s,%s,%d,%f" %
            #       (sample.client_id, sample.absolute_time, sample.relative_time, sample.operation, sample.sample_type,
            #        sample.total_ops, sample.time_period), file=sample_log)

            # once we have seen a new sample type, we stick to it.
            current.maybe_update_sample_type(sample.sample_type)

            # we need to store the total count separately and cannot update `current.total_count` immediately here
            # because we would count all raw samples in `unprocessed` twice. Hence, we'll only update
            # `current.total_count` when we have calculated a new throughput sample.
            count += sample.total_ops
            current.update_interval(sample.absolute_time)

            if current.can_calculate_throughput():
                current.finish_bucket(count)
                task_throughput.append((sample.absolute_time,
                                        sample.relative_time,
                                        current.sample_type,
                                        current.throughput,
                                        # we calculate throughput per second
                                        f"{sample.total_ops_unit}/s"))
            else:
                current.unprocessed.append(sample)

        # also include the last sample if we don't have one for the current sample type, even if it is below the bucket
        # interval (mainly needed to ensure we show throughput data in test mode)
        if last_sample is not None and current.can_add_final_throughput_sample():
            current.finish_bucket(count)
            task_throughput.append((last_sample.absolute_time,
                                    last_sample.relative_time,
                                    current.sample_type,
                                    current.throughput,
                                    f"{last_sample.total_ops_unit}/s"))

        return task_throughput

    def map_task_throughput(self, current_samples):
        throughput = []
        for sample in current_samples:
            throughput.append((sample.absolute_time,
                               sample.relative_time,
                               sample.sample_type,
                               sample.throughput,
                               f"{sample.total_ops_unit}/s"))
        return throughput


class AsyncIoAdapter:
    def __init__(self, cfg, workload, task_allocations, sampler, profile_sampler, cancel, complete, abort_on_error,
                 shared_states=None, feedback_actor=None, error_queue=None, queue_lock=None):
        self.cfg = cfg
        self.workload = workload
        self.task_allocations = task_allocations
        self.sampler = sampler
        self.profile_sampler = profile_sampler
        self.cancel = cancel
        self.complete = complete
        self.abort_on_error = abort_on_error
        self.profiling_enabled = self.cfg.opts("worker_coordinator", "profiling")
        self.assertions_enabled = self.cfg.opts("worker_coordinator", "assertions")
        self.debug_event_loop = self.cfg.opts("system", "async.debug", mandatory=False, default_value=False)
        self.logger = logging.getLogger(__name__)
        self.shared_states = shared_states
        self.feedback_actor = feedback_actor
        self.error_queue = error_queue
        self.queue_lock = queue_lock

    def __call__(self, *args, **kwargs):
        # only possible in Python 3.7+ (has introduced get_running_loop)
        # try:
        #     loop = asyncio.get_running_loop()
        # except RuntimeError:
        #     loop = asyncio.new_event_loop()
        #     asyncio.set_event_loop(loop)
        loop = asyncio.new_event_loop()
        loop.set_debug(self.debug_event_loop)
        loop.set_exception_handler(self._logging_exception_handler)
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run())
        finally:
            loop.close()

    def _logging_exception_handler(self, loop, context):
        self.logger.error("Uncaught exception in event loop: %s", context)

    async def run(self):
        def os_clients(all_hosts, all_client_options):
            opensearch = {}
            for cluster_name, cluster_hosts in all_hosts.items():
                opensearch[cluster_name] = client.OsClientFactory(cluster_hosts, all_client_options[cluster_name]).create_async()
            return opensearch

        # Properly size the internal connection pool to match the number of expected clients but allow the user
        # to override it if needed.
        client_count = len(self.task_allocations)
        opensearch = os_clients(self.cfg.opts("client", "hosts").all_hosts,
                        self.cfg.opts("client", "options").with_max_connections(client_count))

        self.logger.info("Task assertions enabled: %s", str(self.assertions_enabled))
        runner.enable_assertions(self.assertions_enabled)

        aws = []
        # A parameter source should only be created once per task - it is partitioned later on per client.
        params_per_task = {}
        for client_id, task_allocation in self.task_allocations:
            task = task_allocation.task
            if task not in params_per_task:
                param_source = workload.operation_parameters(self.workload, task)
                params_per_task[task] = param_source
            # We cannot use the global client index here because we need to support parallel execution of tasks
            # with multiple clients. Consider the following scenario:
            #
            # * Clients 0-3 bulk index into indexA
            # * Clients 4-7 bulk index into indexB
            #
            # Now we need to ensure that we start partitioning parameters correctly in both cases. And that means we
            # need to start from (client) index 0 in both cases instead of 0 for indexA and 4 for indexB.
            schedule = schedule_for(task_allocation, params_per_task[task])
            async_executor = AsyncExecutor(
                client_id, task, schedule, opensearch, self.sampler, self.profile_sampler, self.cancel, self.complete,
                task.error_behavior(self.abort_on_error), self.cfg, self.shared_states, self.feedback_actor, self.error_queue, self.queue_lock)
            final_executor = AsyncProfiler(async_executor) if self.profiling_enabled else async_executor
            aws.append(final_executor())
        run_start = time.perf_counter()
        try:
            _ = await asyncio.gather(*aws)
        finally:
            run_end = time.perf_counter()
            self.logger.info("Total run duration: %f seconds.", (run_end - run_start))
            await asyncio.get_event_loop().shutdown_asyncgens()
            shutdown_asyncgens_end = time.perf_counter()
            self.logger.info("Total time to shutdown asyncgens: %f seconds.", (shutdown_asyncgens_end - run_end))
            for s in opensearch.values():
                await s.transport.close()
            transport_close_end = time.perf_counter()
            self.logger.info("Total time to close transports: %f seconds.", (shutdown_asyncgens_end - transport_close_end))


class AsyncProfiler:
    def __init__(self, target):
        """
        :param target: The actual executor which should be profiled.
        """
        self.target = target
        self.profile_logger = logging.getLogger("benchmark.profile")

    async def __call__(self, *args, **kwargs):
        # initialize lazily, we don't need it in the majority of cases
        # pylint: disable=import-outside-toplevel
        import yappi
        import io as python_io
        yappi.start()
        try:
            return await self.target(*args, **kwargs)
        finally:
            yappi.stop()
            s = python_io.StringIO()
            yappi.get_func_stats().print_all(out=s, columns={
                0: ("name", 140),
                1: ("ncall", 8),
                2: ("tsub", 8),
                3: ("ttot", 8),
                4: ("tavg", 8)
            })

            profile = "\n=== Profile START ===\n"
            profile += s.getvalue()
            profile += "=== Profile END ==="
            self.profile_logger.info(profile)


class AsyncExecutor:
    def __init__(self, client_id, task, schedule, opensearch, sampler, profile_sampler, cancel, complete, on_error,
                 config=None, shared_states=None, feedback_actor=None, error_queue=None, queue_lock=None):
        """
        Executes tasks according to the schedule for a given operation.
        """
        self.client_id = client_id
        self.task = task
        self.op = task.operation
        self.schedule_handle = schedule
        self.opensearch = opensearch
        self.sampler = sampler
        self.profile_sampler = profile_sampler
        self.cancel = cancel
        self.complete = complete
        self.on_error = on_error
        self.logger = logging.getLogger(__name__)
        self.cfg = config
        self.message_producer = None  # Producer will be lazily created when needed.
        self.shared_states = shared_states
        self.feedback_actor = feedback_actor
        self.error_queue = error_queue
        self.queue_lock = queue_lock
        self.redline_enabled = self.cfg.opts("workload", "redline.test", mandatory=False) if self.cfg else False

        # Client options are fetched once during initialization, not on every request.
        self.client_options = self._get_client_options()
        self.base_timeout = int(self.client_options.get("base_timeout", 10))

        # Variables to keep track of during execution
        self.expected_scheduled_time = 0
        self.sample_type = None
        self.runner = None
        self.task_completes_parent = False

    def _get_client_options(self) -> dict:
        """Get client options from configuration."""
        try:
            if self.cfg is not None:
                client_options_obj = self.cfg.opts("client", "options")
                return getattr(client_options_obj, "all_client_options", {}) or {}
            else:
                return {}
        except exceptions.ConfigError:
            return {}

    async def _wait_for_rampup(self, rampup_wait_time: float) -> None:
        """Wait for the ramp-up phase if needed."""
        if rampup_wait_time:
            self.logger.info("client id [%s] waiting [%.2f]s for ramp-up.", self.client_id, rampup_wait_time)
            await asyncio.sleep(rampup_wait_time)
            self.logger.info("Client id [%s] is running now.", self.client_id)

    async def _prepare_context_manager(self, params: dict):
        """Prepare the appropriate context manager for the request."""
        if params is not None and params.get("operation-type") == "produce-stream-message":
            if self.message_producer is None:
                self.message_producer = await client.MessageProducerFactory.create(params)
            params.update({"message-producer": self.message_producer})
            return self.message_producer.new_request_context()
        else:
            context_manager = self.opensearch["default"].new_request_context()
            if params is not None and params.get("operation-type") == "vector-search":
                available_cores = int(self.cfg.opts("system", "available.cores", mandatory=False,
                                                    default_value=multiprocessing.cpu_count()))
                params.update({"num_clients": self.task.clients, "num_cores": available_cores})
            return context_manager

    async def _execute_request(self, params: dict, expected_scheduled_time: float, total_start: float,
                               client_state: bool) -> dict:
        """Execute a request with timing control and error handling."""
        absolute_expected_schedule_time = total_start + expected_scheduled_time
        throughput_throttled = expected_scheduled_time > 0

        if throughput_throttled:
            rest = absolute_expected_schedule_time - time.perf_counter()
            if rest > 0:
                await asyncio.sleep(rest)

        absolute_processing_start = time.time()
        processing_start = time.perf_counter()
        self.schedule_handle.before_request(processing_start)

        context_manager = await self._prepare_context_manager(params)

        request_start = request_end = client_request_start = client_request_end = None
        total_ops, total_ops_unit, request_meta_data = 0, "ops", {}

        async with context_manager as request_context:
            try:
                total_ops, total_ops_unit, request_meta_data = await asyncio.wait_for(
                    execute_single(
                        self.runner, self.opensearch, params, self.on_error,
                        redline_enabled=self.redline_enabled, client_enabled=client_state
                    ),
                    timeout=self.base_timeout
                )
            except asyncio.TimeoutError:
                self.logger.error("Client %s request timed out after %s s", self.client_id, self.base_timeout)
                request_meta_data = {"success": False, "error-type": "timeout"}

                # Simulate full timing lifecycle
                request_context_holder.on_client_request_start()
                request_context_holder.on_request_start()
                request_context_holder.on_request_end()
                request_context_holder.on_client_request_end()

            # Now safely extract request timings
            request_start = request_context.request_start
            request_end = request_context.request_end
            client_request_start = request_context.client_request_start
            client_request_end = request_context.client_request_end

        # If request failed or timings weren't properly captured, fall back
        if not request_meta_data.get("success") or None in (request_start, request_end, client_request_start,
                                                            client_request_end):
            if request_start is None:
                request_start = processing_start
            if client_request_start is None:
                client_request_start = processing_start
            now = time.perf_counter()
            if request_end is None:
                request_end = now
            if client_request_end is None:
                client_request_end = now

            if not request_meta_data.get("skipped", False):
                error_info = {
                    "client_id": self.client_id,
                    "task": str(self.task),
                    "error_details": request_meta_data
                }
                self.report_error(error_info)

        processing_end = time.perf_counter()

        return {
            "absolute_processing_start": absolute_processing_start,
            "processing_start": processing_start,
            "processing_end": processing_end,
            "request_start": request_start,
            "request_end": request_end,
            "client_request_start": client_request_start,
            "client_request_end": client_request_end,
            "total_ops": total_ops,
            "total_ops_unit": total_ops_unit,
            "request_meta_data": request_meta_data,
            "throughput_throttled": throughput_throttled
        }

    def _process_results(self, result_data: dict, total_start: float, client_state: bool,
                         percent_completed: float, add_profile_metric_sample: bool = False) -> bool:
        """Process results from a request."""
        # Handle cases where the request was skipped (no-op)
        if result_data["request_meta_data"].get("skipped_request"):
            self.schedule_handle.after_request(
                result_data["processing_end"], 0, "ops", {"success": False, "skipped": True}
            )
            return self.complete.is_set()

        service_time = result_data["request_end"] - result_data["request_start"]
        client_processing_time = (result_data["client_request_end"] - result_data[
            "client_request_start"]) - service_time
        processing_time = result_data["processing_end"] - result_data["processing_start"]
        time_period = result_data["request_end"] - total_start

        self.schedule_handle.after_request(
            result_data["processing_end"],
            result_data["total_ops"],
            result_data["total_ops_unit"],
            result_data["request_meta_data"]
        )

        throughput = result_data["request_meta_data"].pop("throughput", None)
        latency = (result_data["request_end"] - (total_start + self.expected_scheduled_time)
                   if result_data["throughput_throttled"] else service_time)

        runner_completed = getattr(self.runner, "completed", False)
        runner_percent_completed = getattr(self.runner, "percent_completed", None)

        if self.task_completes_parent:
            completed = runner_completed
        else:
            completed = self.complete.is_set() or runner_completed

        if completed:
            progress = 1.0
        elif runner_percent_completed is not None:
            progress = runner_percent_completed
        else:
            progress = percent_completed

        if client_state:
            if add_profile_metric_sample:
                self.profile_sampler.add(
                    self.task, self.client_id, self.sample_type,
                    result_data["request_meta_data"],
                    result_data["absolute_processing_start"],
                    result_data["request_start"],
                    time_period,
                    progress,
                    result_data["request_meta_data"].pop("dependent_timing", None))
            else:
                self.sampler.add(
                    self.task, self.client_id, self.sample_type,
                    result_data["request_meta_data"],
                    result_data["absolute_processing_start"],
                    result_data["request_start"],
                    latency, service_time, client_processing_time, processing_time,
                    throughput, result_data["total_ops"], result_data["total_ops_unit"],
                    time_period, progress,
                    result_data["request_meta_data"].pop("dependent_timing", None)
                )
        return completed

    async def _cleanup(self) -> None:
        """Clean up resources after task execution."""
        if self.message_producer is not None:
            await self.message_producer.stop()
            self.message_producer = None

    def report_error(self, error_info: dict) -> None:
        """Report an error to the error queue."""
        if self.error_queue is not None:
            try:
                self.error_queue.put_nowait(error_info)
            except queue.Full:
                self.logger.warning("Error queue full; dropping error from client %s", self.client_id)

    async def __call__(self, *args, **kwargs):
        self.task_completes_parent = self.task.completes_parent
        total_start = time.perf_counter()

        self.logger.debug("Initializing schedule for client id [%s].", self.client_id)
        schedule = self.schedule_handle()
        self.schedule_handle.start()
        rampup_wait_time = self.schedule_handle.ramp_up_wait_time

        await self._wait_for_rampup(rampup_wait_time)

        self.logger.debug("Entering main loop for client id [%s].", self.client_id)
        profile_metrics_sample_count = 0
        try:
            async for expected_scheduled_time, sample_type, percent_completed, runner, params in schedule:
                self.expected_scheduled_time = expected_scheduled_time
                self.sample_type = sample_type
                self.runner = runner

                if self.cancel.is_set():
                    self.logger.info("User cancelled execution.")
                    break

                # `execute_single` will handle the no-op if the client is paused.
                client_state = (self.shared_states or {}).get(self.client_id, True)

                profile_metrics_sample_size = (params or {}).get("profile-metrics-sample-size", None)
                add_profile_metric_sample = profile_metrics_sample_size and profile_metrics_sample_count < profile_metrics_sample_size
                if add_profile_metric_sample:
                    profile_metrics_sample_count += 1
                    params["profile-query"] = True
                elif params:
                    params["profile-query"] = False

                result_data = await self._execute_request(params, expected_scheduled_time, total_start, client_state)

                completed = self._process_results(result_data, total_start, client_state, percent_completed, add_profile_metric_sample)

                if completed:
                    self.logger.info("Task [%s] is considered completed due to external event.", self.task)
                    break
        except BaseException as e:
            self.logger.exception("Could not execute schedule")
            raise exceptions.BenchmarkError(f"Cannot run task [{self.task}]: {e}") from None
        finally:
            if self.task_completes_parent:
                self.logger.info(
                    "Task [%s] completes parent. Client id [%s] is finished executing it and signals completion.",
                    self.task, self.client_id
                )
                self.complete.set()
            await self._cleanup()

request_context_holder = client.RequestContextHolder()


async def execute_single(runner, opensearch, params, on_error, redline_enabled=False, client_enabled=True):
    """
    Invokes the given runner once and provides the runner's return value in a uniform structure.

    :return: a triple of: total number of operations, unit of operations, a dict of request meta data (may be None).
    """
    # pylint: disable=import-outside-toplevel
    import opensearchpy
    fatal_error = False
    if client_enabled:
        try:
            async with runner:
                return_value = await runner(opensearch, params)
            if isinstance(return_value, tuple) and len(return_value) == 2:
                total_ops, total_ops_unit = return_value
                request_meta_data = {"success": True}
            elif isinstance(return_value, dict):
                total_ops = return_value.pop("weight", 1)
                total_ops_unit = return_value.pop("unit", "ops")
                request_meta_data = return_value
                if "success" not in request_meta_data:
                    request_meta_data["success"] = True
            else:
                total_ops = 1
                total_ops_unit = "ops"
                request_meta_data = {"success": True}
        except opensearchpy.TransportError as e:
            request_context_holder.on_client_request_end()
            # we *specifically* want to distinguish connection refused (a node died?) from connection timeouts
            # pylint: disable=unidiomatic-typecheck
            if type(e) is opensearchpy.ConnectionError:
                fatal_error = True

            total_ops = 0
            total_ops_unit = "ops"
            request_meta_data = {
                "success": False,
                "error-type": "transport"
            }
            # The OS client will sometimes return string like "N/A" or "TIMEOUT" for connection errors.
            if isinstance(e.status_code, int):
                request_meta_data["http-status"] = e.status_code
            # connection timeout errors don't provide a helpful description
            if isinstance(e, opensearchpy.ConnectionTimeout):
                request_meta_data["error-description"] = "network connection timed out"
            elif e.info:
                request_meta_data["error-description"] = f"{e.error} ({e.info})"
            else:
                if isinstance(e.error, bytes):
                    error_description = e.error.decode("utf-8")
                else:
                    error_description = str(e.error)
                request_meta_data["error-description"] = error_description
        except KeyError as e:
            request_context_holder.on_client_request_end()
            logging.getLogger(__name__).exception("Cannot execute runner [%s]; most likely due to missing parameters.", str(runner))
            msg = "Cannot execute [%s]. Provided parameters are: %s. Error: [%s]." % (str(runner), list(params.keys()), str(e))
            if not redline_enabled:
                console.error(msg)
                raise exceptions.SystemSetupError(msg)

        if not request_meta_data["success"]:
            if on_error == "abort" or fatal_error:
                msg = "Request returned an error. Error type: %s" % request_meta_data.get("error-type", "Unknown")
                description = request_meta_data.get("error-description")
                if description:
                    msg += ", Description: %s" % description
                    if not redline_enabled:
                        console.error(msg)
                if not redline_enabled:
                    raise exceptions.BenchmarkAssertionError(msg)

            if 'error-description' in request_meta_data:
                try:
                    error_metadata = json.loads(request_meta_data["error-description"])
                    # parse error-description metadata
                    opensearch_operation_error = parse_error(error_metadata)
                    if not redline_enabled:
                        console.error(opensearch_operation_error.get_error_message())
                except Exception as e:
                    # error-description is not a valid json so we just print it
                    if not redline_enabled:
                        console.error(request_meta_data["error-description"])
                if not redline_enabled:
                    logging.getLogger(__name__).error(request_meta_data["error-description"])
    else:
        request_context_holder.on_client_request_start()
        request_context_holder.on_request_start()
        total_ops = 0
        total_ops_unit = "ops"
        request_meta_data = {
            "success": True,
            "skipped_request": True
        }
        request_context_holder.on_request_end()
        request_context_holder.on_client_request_end()
    return total_ops, total_ops_unit, request_meta_data


class JoinPoint:
    def __init__(self, id, clients_executing_completing_task=None):
        """

        :param id: The join point's id.
        :param clients_executing_completing_task: An array of client indices which execute a task that can prematurely complete its parent
        element. Provide 'None' or an empty array if no task satisfies this predicate.
        """
        if clients_executing_completing_task is None:
            clients_executing_completing_task = []
        self.id = id
        self.clients_executing_completing_task = clients_executing_completing_task
        self.num_clients_executing_completing_task = len(clients_executing_completing_task)
        self.preceding_task_completes_parent = self.num_clients_executing_completing_task > 0

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, type(self)) and self.id == other.id

    def __repr__(self, *args, **kwargs):
        return "JoinPoint(%s)" % self.id


class TaskAllocation:
    def __init__(self, task, client_index_in_task, global_client_index, total_clients):
        """
        :param task: The current task which is always a leaf task.
        :param client_index_in_task: The task-specific index for the allocated client.
        :param global_client_index:  The globally unique index for the allocated client across
                                     all concurrently executed tasks.
        :param total_clients: The total number of clients executing tasks concurrently.
        """
        self.task = task
        self.client_index_in_task = client_index_in_task
        self.global_client_index = global_client_index
        self.total_clients = total_clients

    def __hash__(self):
        return hash(self.task) ^ hash(self.global_client_index)

    def __eq__(self, other):
        return isinstance(other, type(self)) and self.task == other.task and self.global_client_index == other.global_client_index

    def __repr__(self, *args, **kwargs):
        return f"TaskAllocation [{self.client_index_in_task}/{self.task.clients}] for {self.task} " \
               f"and [{self.global_client_index}/{self.total_clients}] in total"


class Allocator:
    """
    Decides which operations runs on which client and how to partition them.
    """

    def __init__(self, schedule):
        self.schedule = schedule

    @property
    def allocations(self):
        """
        Calculates an allocation matrix consisting of two dimensions. The first dimension is the client. The second dimension are the task
         this client needs to run. The matrix shape is rectangular (i.e. it is not ragged). There are three types of entries in the matrix:

          1. Normal tasks: They need to be executed by a client.
          2. Join points: They are used as global coordination points which all clients need to reach until the benchmark can go on. They
                          indicate that a client has to wait until the master signals it can go on.
          3. `None`: These are inserted by the allocator to keep the allocation matrix rectangular. Clients have to skip `None` entries
                     until one of the other entry types are encountered.

        :return: An allocation matrix with the structure described above.
        """
        max_clients = self.clients
        allocations = [None] * max_clients
        for client_index in range(max_clients):
            allocations[client_index] = []
        join_point_id = 0
        # start with an artificial join point to allow master to coordinate that all clients start at the same time
        next_join_point = JoinPoint(join_point_id)
        for client_index in range(max_clients):
            allocations[client_index].append(next_join_point)
        join_point_id += 1

        for task in self.schedule:
            start_client_index = 0
            clients_executing_completing_task = []
            for sub_task in task:
                for client_index in range(start_client_index, start_client_index + sub_task.clients):
                    physical_client_index = client_index % max_clients
                    if sub_task.completes_parent:
                        clients_executing_completing_task.append(physical_client_index)
                    ta = TaskAllocation(task = sub_task,
                                        client_index_in_task = client_index - start_client_index,
                                        global_client_index=client_index,
                                        # if task represents a parallel structure this is the total number of clients
                                        # executing sub-tasks concurrently.
                                        total_clients=task.clients)
                    allocations[physical_client_index].append(ta)
                start_client_index += sub_task.clients

            # uneven distribution between tasks and clients, e.g. there are 5 (parallel) tasks but only 2 clients. Then, one of them
            # executes three tasks, the other one only two. So we need to fill in a `None` for the second one.
            if start_client_index % max_clients > 0:
                # pin the index range to [0, max_clients). This simplifies the code below.
                start_client_index = start_client_index % max_clients
                for client_index in range(start_client_index, max_clients):
                    allocations[client_index].append(None)

            # let all clients join after each task, then we go on
            next_join_point = JoinPoint(join_point_id, clients_executing_completing_task)
            for client_index in range(max_clients):
                allocations[client_index].append(next_join_point)
            join_point_id += 1
        return allocations

    @property
    def join_points(self):
        """
        :return: A list of all join points for this allocations.
        """
        return [allocation for allocation in self.allocations[0] if isinstance(allocation, JoinPoint)]

    @property
    def tasks_per_joinpoint(self):
        """

        Calculates a flat list of all tasks that are run in between join points.

        Consider the following schedule (2 clients):

        1. task1 and task2 run by both clients in parallel
        2. join point
        3. task3 run by client 1
        4. join point

        The results in: [{task1, task2}, {task3}]

        :return: A list of sets containing all tasks.
        """
        tasks = []
        current_tasks = set()

        allocs = self.allocations
        # assumption: the shape of allocs is rectangular (i.e. each client contains the same number of elements)
        for idx in range(0, len(allocs[0])):
            for client in range(0, self.clients):
                allocation = allocs[client][idx]
                if isinstance(allocation, TaskAllocation):
                    current_tasks.add(allocation.task)
                elif isinstance(allocation, JoinPoint) and len(current_tasks) > 0:
                    tasks.append(current_tasks)
                    current_tasks = set()

        return tasks

    @property
    def clients(self):
        """
        :return: The maximum number of clients involved in executing the given schedule.
        """
        max_clients = 1
        for task in self.schedule:
            max_clients = max(max_clients, task.clients)
        return max_clients


#######################################
#
# Scheduler related stuff
#
#######################################


# Runs a concrete schedule on one worker client
# Needs to determine the runners and concrete iterations per client.
def schedule_for(task_allocation, parameter_source):
    """
    Calculates a client's schedule for a given task.

    :param task: The task that should be executed.
    :param client_index: The current client index.  Must be in the range [0, `task.clients').
    :param parameter_source: The parameter source that should be used for this task.
    :return: A generator for the operations the given client needs to perform for this task.
    """
    logger = logging.getLogger(__name__)
    task = task_allocation.task
    op = task.operation
    sched = scheduler.scheduler_for(task)

    client_index = task_allocation.client_index_in_task
    # guard all logging statements with the client index and only emit them for the first client. This information is
    # repetitive and may cause issues in thespian with many clients (an excessive number of actor messages is sent).
    if client_index == 0:
        logger.info("Choosing [%s] for [%s].", sched, task)
    runner_for_op = runner.runner_for(op.type)
    params_for_op = parameter_source.partition(client_index, task.clients)
    if hasattr(sched, "parameter_source"):
        if client_index == 0:
            logger.debug("Setting parameter source [%s] for scheduler [%s]", params_for_op, sched)
        sched.parameter_source = params_for_op

    if requires_time_period_schedule(task, runner_for_op, params_for_op):
        warmup_time_period = task.warmup_time_period if task.warmup_time_period else 0
        if client_index == 0:
            logger.info("Creating time-period based schedule with [%s] distribution for [%s] with a warmup period of [%s] "
                        "seconds and a time period of [%s] seconds.", task.schedule, task.name,
                        str(warmup_time_period), str(task.time_period))
        loop_control = TimePeriodBased(warmup_time_period, task.time_period)
    else:
        warmup_iterations = task.warmup_iterations if task.warmup_iterations else 0
        if task.iterations:
            iterations = task.iterations
        elif params_for_op.infinite:
            # this is usually the case if the parameter source provides a constant
            iterations = 1
        else:
            iterations = None
        if client_index == 0:
            logger.info("Creating iteration-count based schedule with [%s] distribution for [%s] with [%s] warmup "
                        "iterations and [%s] iterations.", task.schedule, task.name, str(warmup_iterations), str(iterations))
        loop_control = IterationBased(warmup_iterations, iterations)

    if client_index == 0:
        if loop_control.infinite:
            logger.info("Parameter source will determine when the schedule for [%s] terminates.", task.name)
        else:
            logger.info("%s schedule will determine when the schedule for [%s] terminates.", str(loop_control), task.name)

    return ScheduleHandle(task_allocation, sched, loop_control, runner_for_op, params_for_op)


def requires_time_period_schedule(task, task_runner, params):
    if task.warmup_time_period is not None or task.time_period is not None:
        return True
    # user has explicitly requested iterations
    if task.warmup_iterations is not None or task.iterations is not None:
        return False
    # the runner determines completion
    if task_runner.completed is not None:
        return True
    # If the parameter source ends after a finite amount of iterations, we will run with a time-based schedule
    return not params.infinite


class ScheduleHandle:
    def __init__(self, task_allocation, sched, task_progress_control, runner, params):
        """
        Creates a generator that will yield individual task invocations for the provided schedule.

        :param task_allocation: The task allocation for which the schedule is generated.
        :param sched: The scheduler for this task.
        :param task_progress_control: Controls how and how often this generator will loop.
        :param runner: The runner for a given operation.
        :param params: The parameter source for a given operation.
        :return: A generator for the corresponding parameters.
        """
        self.task_allocation = task_allocation
        self.sched = sched
        self.task_progress_control = task_progress_control
        self.runner = runner
        self.params = params
        # TODO: Can we offload the parameter source execution to a different thread / process? Is this too heavy-weight?
        # from concurrent.futures import ThreadPoolExecutor
        # import asyncio
        # self.io_pool_exc = ThreadPoolExecutor(max_workers=1)
        # self.loop = asyncio.get_event_loop()
    @property
    def ramp_up_wait_time(self):
        """
        :return: the number of seconds to wait until this client should start so load can gradually ramp-up.
        """
        ramp_up_time_period = self.task_allocation.task.ramp_up_time_period
        if ramp_up_time_period:
            return ramp_up_time_period * (self.task_allocation.global_client_index / self.task_allocation.total_clients)
        else:
            return 0

    def start(self):
        self.task_progress_control.start()

    def before_request(self, now):
        self.sched.before_request(now)

    def after_request(self, now, weight, unit, request_meta_data):
        self.sched.after_request(now, weight, unit, request_meta_data)

    async def __call__(self):
        next_scheduled = 0
        if self.task_progress_control.infinite:
            param_source_knows_progress = hasattr(self.params, "percent_completed")
            while True:
                try:
                    next_scheduled = self.sched.next(next_scheduled)
                    # does not contribute at all to completion. Hence, we cannot define completion.
                    percent_completed = self.params.percent_completed if param_source_knows_progress else None
                    # current_params = await self.loop.run_in_executor(self.io_pool_exc, self.params.params)
                    yield (next_scheduled, self.task_progress_control.sample_type, percent_completed, self.runner,
                           self.params.params())
                    self.task_progress_control.next()
                except StopIteration:
                    return
        else:
            while not self.task_progress_control.completed:
                try:
                    next_scheduled = self.sched.next(next_scheduled)
                    #current_params = await self.loop.run_in_executor(self.io_pool_exc, self.params.params)
                    yield (next_scheduled,
                           self.task_progress_control.sample_type,
                           self.task_progress_control.percent_completed,
                           self.runner,
                           self.params.params())
                    self.task_progress_control.next()
                except StopIteration:
                    return


class TimePeriodBased:
    def __init__(self, warmup_time_period, time_period):
        self._warmup_time_period = warmup_time_period
        self._time_period = time_period
        if warmup_time_period is not None and time_period is not None:
            self._duration = self._warmup_time_period + self._time_period
        else:
            self._duration = None
        self._start = None
        self._now = None

    def start(self):
        self._now = time.perf_counter()
        self._start = self._now

    @property
    def _elapsed(self):
        return self._now - self._start

    @property
    def sample_type(self):
        return metrics.SampleType.Warmup if self._elapsed < self._warmup_time_period else metrics.SampleType.Normal

    @property
    def infinite(self):
        return self._time_period is None

    @property
    def percent_completed(self):
        return self._elapsed / self._duration

    @property
    def completed(self):
        return self._now >= (self._start + self._duration)

    def next(self):
        self._now = time.perf_counter()

    def __str__(self):
        return "time-period-based"


class IterationBased:
    def __init__(self, warmup_iterations, iterations):
        self._warmup_iterations = warmup_iterations
        self._iterations = iterations
        if warmup_iterations is not None and iterations is not None:
            self._total_iterations = self._warmup_iterations + self._iterations
            if self._total_iterations == 0:
                raise exceptions.BenchmarkAssertionError("Operation must run at least for one iteration.")
        else:
            self._total_iterations = None
        self._it = None

    def start(self):
        self._it = 0

    @property
    def sample_type(self):
        return metrics.SampleType.Warmup if self._it < self._warmup_iterations else metrics.SampleType.Normal

    @property
    def infinite(self):
        return self._iterations is None

    @property
    def percent_completed(self):
        return (self._it + 1) / self._total_iterations

    @property
    def completed(self):
        return self._it >= self._total_iterations

    def next(self):
        self._it += 1

    def __str__(self):
        return "iteration-count-based"
