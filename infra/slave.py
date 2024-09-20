#!/usr/bin/env python


import beanstalkc
import boto
import collections
import io
import errno
import fcntl
import glob2
import logging
import os
import urllib.request, urllib.parse, urllib.error
import urllib.request, urllib.error, urllib.parse
import re
import select
import shutil
import sys
try:
  import simplejson as json
except:
  import json
import signal
import subprocess
import sys
import threading
import time
import zipfile

from config import Config
import dist_test
import file_path

LOG = None

# The number of times each task will retry when trying to download its
# dependencies.
NUM_DOWNLOAD_ATTEMPTS_PER_TASK = 3

class RetryCache(object):
  """Time-based and count-based cache to avoid running retried tasks
  again on the same slave. If a slave sees a retry it submitted, it
  puts it back into beanstalk and does a short sleep in the hope that
  another slave dequeues it.

  This cache tracks the number of times that a given task has been retried
  by this slave. When the number of times reaches a threshold, the task
  is evicted from the cache, letting the task run on the same slave.
  This prevents livelock.

  Otherwise, the cache is evicted based on oldest insertion time."""

  def __init__(self, max_size=100, max_count=10):
    """Create a new RetryCache.
    
    max_size: maximum number of items in the cache.
    max_count: maximum number of touches before an item expires."""
    self.cache = collections.OrderedDict()
    self.max_size = max_size
    self.max_count = max_count

  def get(self, item):
    if not item in list(self.cache.keys()):
      return None
    count = self.cache[item]
    if count > self.max_count:
      LOG.debug("Item %s hit max_count of %d, evicting from cache", item, self.max_count)
      del self.cache[item]
    else:
      self.cache[item] += 1

    return item

  def put(self, item):
    if len(list(self.cache.keys())) == self.max_size:
      LOG.debug("Cache is at capacity %d, evicting oldest item %s", self.max_size, item)
      self.cache.popitem()
    self.cache[item] = 0

class Slave(object):

  def __init__(self, config):
    self.config = config
    self.config.ensure_isolate_configured()
    self.config.ensure_dist_test_configured()
    self.task_queue = dist_test.TaskQueue(self.config)
    self.results_store = dist_test.ResultsStore(self.config)
    self.cache_dir = self._get_exclusive_cache_dir()
    self.cur_task = None
    self.is_busy = False
    self.retry_cache = RetryCache()

  def _get_exclusive_cache_dir(self):
    for i in range(0, 16):
      dir = "%s.%d" % (self.config.ISOLATE_CACHE_DIR, i)
      if not os.path.isdir(dir):
        os.makedirs(dir)
      self._lockfile = file(dir + ".lock", "w")
      try:
        fcntl.lockf(self._lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
      except IOError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
          LOG.info("Another slave already using cache dir %s", dir)
          self._lockfile.close()
          continue
        raise
      # Succeeded in locking
      LOG.info("Acquired lock on cache dir %s", dir)
      return dir
    raise Exception("Unable to lock any cache dir %s.<int>" %
        self.config.ISOLATE_CACHE_DIR)

  def _set_flags(self, f):
    fd = f.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

  def make_archive(self, task, test_dir):
    # Return early if no test_dir is specified
    if test_dir is None:
      return None
    # Return early if there are no globs specified
    if task.task.artifact_archive_globs is None or len(task.task.artifact_archive_globs) == 0:
      return None
    all_matched = set()
    total_size = 0
    for g in task.task.artifact_archive_globs:
      try:
          matched = glob2.iglob(test_dir + "/" + g)
          for m in matched:
            canonical = os.path.realpath(m)
            if not canonical.startswith(test_dir):
              LOG.warn("Glob %s matched file outside of test_dir, skipping: %s" % (g, canonical))
              continue
            total_size += os.stat(canonical).st_size
            all_matched.add(canonical)
      except Exception as e:
        LOG.warn("Error while globbing %s: %s" % (g, e))

    if len(all_matched) == 0:
      return None
    max_size = 200*1024*1024 # 200MB max uncompressed size
    if total_size > max_size:
      # If size exceeds the maximum size, upload a zip with an error message instead
      LOG.info("Task %s generated too many bytes of matched artifacts (%d > %d)," \
               + "uploading archive with error message instead.",
              task.task.get_id(), total_size, max_size)
      archive_buffer = io.StringIO()
      with zipfile.ZipFile(archive_buffer, "w") as myzip:
        myzip.writestr("_ARCHIVE_TOO_BIG_",
                       "Size of matched uncompressed test artifacts exceeded maximum size" \
                       + "(%d bytes > %d bytes)!" % (total_size, max_size))
      return archive_buffer

    # Write out the archive
    archive_buffer = io.StringIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as myzip:
      for m in all_matched:
        arcname = os.path.relpath(m, test_dir)
        while arcname.startswith("/"):
          arcname = arcname[1:]
        myzip.write(m, arcname)

    return archive_buffer

  def download_task_files(self, task, test_dir):
    """
    Download all of the files associated with 'task' into 'test_dir'.
    The directory is expected to already exist.
    """
    env = os.environ.copy()
    # Make isolateserver run in 'bot' mode. This prevents it from trying
    # to use oauth to authenticate.
    env['SWARMING_HEADLESS'] = '1'

    download_cmd_base = [os.path.join(self.config.ISOLATE_HOME, "isolateserver.py"),
           "download",
           "--isolate-server=%s" % self.config.ISOLATE_SERVER,
           "--cache=%s" % self.cache_dir,
           "--verbose"]

    LOG.info("Downloading files from isolate...")
    download_cmd = download_cmd_base + [
           "-i", task.task.isolate_hash,
           "--target", test_dir]
    rc, stdout, stderr = self.run_command_and_touch_task(
           download_cmd, task, timeout=600, env=env)
    if rc != 0:
      raise Exception("failed to download task files: %s" % stderr)

    # The above doesn't download the '.isolated' file itself. We need
    # this file since it describes the task working directory, command
    # line, etc.
    LOG.info("Downloading isolated file from isolate...")
    isolated_path = os.path.join(test_dir, "task.isolated")
    download_cmd = download_cmd_base + ["-f", task.task.isolate_hash, isolated_path]
    rc, stdout, stderr = self.run_command_and_touch_task(
           download_cmd, task, timeout=600, env=env)
    if rc != 0:
      raise Exception("failed to download task files: %s" % stderr)

    # We expect to have all of the files that we download writable, but
    # 'isolateserver.py download' defaults to not writable.
    file_path.make_tree_writeable(test_dir)
    return json.load(file(isolated_path))

  def download_task_files_with_retries(self, task, test_dir):
    """ Calls download_task_files(...) with automatic retries on failure """
    for rem_attempts in reversed(range(NUM_DOWNLOAD_ATTEMPTS_PER_TASK)):
      try:
        return self.download_task_files(task, test_dir)
      except:
        LOG.warning("failed to download task files. %d tries remaining" % rem_attempts, exc_info=True)
        if rem_attempts == 0:
          raise
        # Recreate the target directory since some files may have been downloaded
        # in the first attempt.
        shutil.rmtree(test_dir)
        os.makedirs(test_dir)
        time.sleep(5)

  def run_task(self, task):
    """ Download the files, run the task, and upload results. """
    if not self.results_store.mark_task_running(task.task):
      LOG.info("Task %s canceled", task.task.description)
      return

    start_time = time.time()
    stdout = None
    stderr = None
    rc = None
    downloaded = False
    artifact_archive = None

    # First download everything.
    test_dir = file_path.make_temp_dir("dist-test-task", self.cache_dir)
    try:
      isolated_info = self.download_task_files_with_retries(task, test_dir)
      downloaded = True
    except Exception as e:
      # If we fail to download, make sure to mark the task as failed.
      # It's possible that the isolate file itself is invalid, in which
      # case we don't want the task to get "stuck" in the queue forever
      # bouncing among slaves.
      rc = -2
      stderr = str(e)

    # Then run the actual task, unless it already failed downloading above.
    if downloaded:
      rel_cwd = isolated_info.get('relative_cwd', '')
      if task.task.docker_image:
        cmd = ["docker", "run",
               # Map the test dir into /isolate-dir in the docker container.
               "--volume", "%s:/isolate-dir" % test_dir,
               "--workdir", os.path.join("/isolate-dir", rel_cwd),
               "--user", str(os.geteuid()),
               task.task.docker_image] + isolated_info['command']
        # No need to run 'docker' with any particular cwd -- the above command line
        # sets the appropriate within-container cwd.
        cwd = None
      else:
        cmd = isolated_info['command']
        cwd = os.path.join(test_dir, rel_cwd)

      # The command is always a path to an executable in the downloaded bundle
      # rather than something like 'bash' expected to be on the PATH. However,
      # '.' isn't usually on the path, so we need to ensure that the command
      # is an absoluate path.
      file_path.ensure_command_has_abs_path(cmd, cwd)
      rc, stdout, stderr = self.run_command_and_touch_task(
          cmd, task,
          timeout=task.task.timeout,
          cwd=cwd)

      # Don't upload logs from successful builds
      if rc == 0:
        stdout = None
        stderr = None

      artifact_archive = self.make_archive(task, test_dir)

    end_time = time.time()
    duration_secs = end_time - start_time

    self.results_store.mark_task_finished(task.task,
                                          result_code=rc,
                                          stdout=stdout,
                                          stderr=stderr,
                                          artifact_archive=artifact_archive,
                                          duration_secs=duration_secs)

    # Do cleanup of temp files
    if test_dir is not None:
      LOG.info("Removing test directory %s" % test_dir)
      shutil.rmtree(test_dir)
    if artifact_archive is not None:
      artifact_archive.close()

    if rc != 0:
      # If there have been too many failures, cancel the job
      num_failed = self.results_store.count_num_failed_tasks(task.task)
      if num_failed > 100:
        LOG.info("Job %s has too many failed tasks (%d), cancelling" % (task.task.job_id, num_failed))
        self.cancel_job(task.task.job_id)
      # Retry if non-zero exit code and have retries remaining
      elif task.task.attempt < task.task.max_retries:
        self.submit_retry_task(task)


  def run_command_and_touch_task(self, cmd, task, timeout, **kwargs):
    """
    Run the command 'cmd' with the given timeout 'timeout'.

    While the command is running, periodically touches 'task in the
    queue so that it doesn't get re-assigned to another slave.

    Parameters
    ----------
    cmd : array
        The command to run (suitable for passing to subprocess.Popen)
    task : Task
        The task to periodically touch.
    timeout : int
        The timeout with which to run the given command.
    """
    LOG.info("Running command: %s", repr(cmd))
    p = subprocess.Popen(
      cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    pipes = [p.stdout, p.stderr]
    self._set_flags(p.stdout)
    self._set_flags(p.stderr)

    stdout = ""
    stderr = ""

    last_touch = time.time()
    kill_term_time = last_touch + timeout
    kill_kill_time = kill_term_time + 5
    while True:
      rlist, wlist, xlist = select.select(pipes, [], pipes, 2)
      if p.stdout in rlist:
        x = p.stdout.read(1024 * 1024)
        stdout += x
      if p.stderr in rlist:
        x = p.stderr.read(1024 * 1024)
        stderr += x
      if xlist or p.poll() is not None:
        break
      now = time.time()
      if timeout > 0 and now > kill_term_time:
        LOG.info("Task timed out: " + task.task.description)
        stderr += "\n------\nKilling task after %d seconds" % timeout
        p.terminate()
      if timeout > 0 and now > kill_kill_time:
        LOG.info("Task did not exit after SIGTERM. Sending SIGKILL")
        p.kill()

      if time.time() - last_touch > 10:
        LOG.info("Still running: " + task.task.description)
        try:
          task.bs_elem.touch()
        except:
          LOG.info("Could not touch beanstalk queue elem", exc_info=True)
          pass
        last_touch = time.time()

    return p.wait(), stdout, stderr

  def cancel_job(self, job_id):
    url = self.config.DIST_TEST_MASTER + "/cancel_job?job_id=" + job_id
    LOG.info("Cancelling job %s via url %s" % (job_id, url))
    try:
        result_str = urllib.request.urlopen(url).read()
        result = json.loads(result_str)
        if result.get('status') != 'SUCCESS':
            sys.stderr.write("Unable to cancel job %s: %s" % (job_id, repr(result)))
    except Exception as e:
        sys.stderr.write("Unable to cancel job %s: %s\n" % (job_id, e))

  def submit_retry_task(self, task):
    task_json = task.task.to_json()
    form_data = urllib.parse.urlencode({'task_json': task_json})
    url = self.config.DIST_TEST_MASTER + "/retry_task"
    result_str = urllib.request.urlopen(url, data=form_data).read()
    result = json.loads(result_str)
    if result.get('status') != 'SUCCESS':
      sys.stderr.write("Unable to submit retry task: %s\n" % repr(result))
    # Add to the retry cache for anti-affinity
    self.retry_cache.put(task.task.get_retry_id())

  def handle_sigterm(self):
    logging.error("caught SIGTERM! shutting down")
    if self.cur_task is not None:
      logging.warning("releasing running job")
      self.cur_task.bs_elem.release()
    os._exit(0)

  def run(self):
    while True:
      try:
        logging.info("waiting for next task...")
        self.is_busy = False
        self.cur_task = self.task_queue.reserve_task()
      except Exception as e:
        LOG.warning("Failed to reserve job: %s" % str(e))
        time.sleep(1)
        continue

      LOG.info("got task: %s", self.cur_task.task.to_json())

      if self.retry_cache.get(self.cur_task.task.get_retry_id()) is not None:
        sleep_time = 5
        LOG.info("Got a retry task submitted by this slave, releasing it and sleeping %d s...", sleep_time)
        self.cur_task.bs_elem.release()
        time.sleep(sleep_time)
        continue

      self.is_busy = True
      self.run_task(self.cur_task)
      try:
        logging.info("task complete")
        self.cur_task.bs_elem.delete()
      except Exception as e:
        LOG.warning("Failed to delete job: %s" % str(e))
      finally:
        self.cur_task = None


def main():
  global LOG

  config = Config()
  LOG = logging.getLogger('dist_test.slave')
  dist_test.configure_logger(LOG, config.SLAVE_LOG)
  config.configure_auth()

  LOG.info("Starting slave")
  s = Slave(config)
  signal.signal(signal.SIGTERM, lambda sig, stack: s.handle_sigterm())
  s.run()

if __name__ == "__main__":
  main()
