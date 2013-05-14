from __future__ import absolute_import
import os
import sys
import errno
import signal
import socket
import logging
import itertools
import subprocess
from time import time, sleep
from setproctitle import setproctitle
from kuyruk.version import __version__
from kuyruk.process import KuyrukProcess

logger = logging.getLogger(__name__)


class Master(KuyrukProcess):
    """
    Master worker implementation that coordinates queue workers.

    """
    def __init__(self, config):
        super(Master, self).__init__(config)
        self.workers = []

    def run(self):
        super(Master, self).run()
        setproctitle('kuyruk: master')
        self.maybe_start_manager_thread()
        self.start_workers()
        self.wait_for_workers()
        logger.debug('End run master')

    def start_workers(self):
        """Start a new worker for each queue"""
        queues = self.get_queues()
        queues = parse_queues_str(queues)
        logger.info('Starting to work on queues: %s', queues)
        for queue in queues:
            self.spawn_new_worker(queue)

    def get_queues(self):
        """Return queues string."""
        hostname = socket.gethostname()
        try:
            return self.config.WORKERS[hostname]
        except KeyError:
            logger.warning('No queues specified for host %r. '
                           'Listening on default queue: "kuyruk"', hostname)
            return 'kuyruk'

    def stop_workers(self, kill=False):
        """Send stop signal to all workers."""
        for worker in self.workers:
            os.kill(worker.pid, signal.SIGKILL if kill else signal.SIGTERM)

    def wait_for_workers(self):
        """Loop until any of the self.workers is alive.
        If a worker is dead and Kuyruk is running state, spawn a new worker.

        """
        start = time()
        any_alive = True
        while any_alive:
            any_alive = False
            for worker in list(self.workers):
                if worker.is_alive():
                    any_alive = True
                elif not self.shutdown_pending.is_set():
                    worker.kill_pg()
                    self.respawn_worker(worker)
                    any_alive = True
                else:
                    self.workers.remove(worker)

            if any_alive:
                logger.debug("Waiting for workers... "
                             "%i seconds passed" % (time() - start))
                sleep(1)

    def respawn_worker(self, worker):
        """Spawn a new process with parameters same as the old worker."""
        logger.debug("Spawning new worker")
        self.spawn_new_worker(worker.queue)
        self.workers.remove(worker)
        logger.debug(self.workers)

    def spawn_new_worker(self, queue):
        worker = WorkerProcess(self.config, queue)
        worker.start()
        self.workers.append(worker)

    def register_signals(self):
        super(Master, self).register_signals()
        signal.signal(signal.SIGTERM, self.handle_sigterm)
        signal.signal(signal.SIGQUIT, self.handle_sigquit)
        signal.signal(signal.SIGABRT, self.handle_sigabrt)

    def handle_sigterm(self, signum, frame):
        logger.warning("Handling SIGTERM")
        self.warm_shutdown()

    def handle_sigquit(self, signum, frame):
        logger.warning("Handling SIGQUIT")
        self.cold_shutdown()

    def handle_sigabrt(self, signum, frame):
        logger.warning("Handling SIGABRT")
        self.abort()

    def warm_shutdown(self, sigint=False):
        super(Master, self).warm_shutdown(sigint)
        if not sigint:
            self.stop_workers()

    def cold_shutdown(self):
        logger.warning("Cold shutdown")
        self.shutdown_pending.set()
        self.stop_workers(kill=True)

    def abort(self):
        """Exit immediately making workers orphan."""
        logger.warning("Aborting")
        os._exit(1)

    def get_stats(self):
        """Generate stats to be sent to manager."""
        return {
            'type': 'master',
            'hostname': socket.gethostname(),
            'uptime': self.uptime,
            'pid': os.getpid(),
            'version': __version__,
            'workers': len(self.workers),
            'load': os.getloadavg(),
        }


class WorkerProcess(object):

    def __init__(self, config, queue):
        self.config = config
        self.queue = queue
        self.popen = None

    def start(self):
        """Runs the worker by starting another Python interpreter."""
        command = [sys.executable, '-u', '-m', 'kuyruk.__main__']

        # Pass each config value as command line option
        from .__main__ import to_option
        for key, value in vars(self.config).iteritems():
            if key.isupper():
                command.extend([to_option(key), str(value)])

        command.extend(['worker', '--queue', self.queue])
        self.popen = subprocess.Popen(
            command, stdout=sys.stdout, stderr=sys.stderr, bufsize=1)

    @property
    def pid(self):
        return self.popen.pid

    def is_alive(self):
        self.popen.poll()
        return self.popen.returncode is None

    def kill_pg(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError as e:
            if e.errno != errno.ESRCH:  # No such process
                raise


def parse_queues_str(s):
    """Parse command line queues string.

    :param s: Command line or configuration queues string
    :return: list of queue names
    """
    queues = (q.strip() for q in s.split(','))
    queues = itertools.chain.from_iterable(parse_count(q) for q in queues)
    return [parse_local(q) for q in queues]


def parse_count(q):
    parts = q.split('*', 1)
    if len(parts) > 1:
        try:
            return int(parts[0]) * [parts[1]]
        except ValueError:
            return int(parts[1]) * [parts[0]]
    return [parts[0]]


def parse_local(q):
    if q.startswith('@'):
        return "%s_%s" % (q[1:], socket.gethostname())
    return q
