# Copyright 2014 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This modules implements the engine for building packages in parallel"""

import operator
import os
import stat
import sys
import time
import yaml

from multiprocessing import cpu_count
from threading import Lock
try:
    # Python3
    from queue import Queue
    from queue import Empty
except ImportError:
    # Python2
    from Queue import Queue
    from Queue import Empty

try:
    from catkin_pkg.packages import find_packages
    from catkin_pkg.topological_order import topological_order_packages
except ImportError as e:
    sys.exit(
        'ImportError: "from catkin_pkg.topological_order import '
        'topological_order" failed: %s\nMake sure that you have installed '
        '"catkin_pkg", and that it is up to date and on the PYTHONPATH.' % e
    )

from catkin_tools.notifications import notify

from catkin_tools.common import disable_wide_log
from catkin_tools.common import FakeLock
from catkin_tools.common import format_time_delta
from catkin_tools.common import format_time_delta_short
from catkin_tools.common import get_cached_recursive_build_depends_in_workspace
from catkin_tools.common import get_recursive_run_depends_in_workspace
from catkin_tools.common import is_tty
from catkin_tools.common import log
from catkin_tools.common import remove_ansi_escape
from catkin_tools.common import terminal_width
from catkin_tools.common import wide_log

from catkin_tools.make_jobserver import jobserver_max_jobs
from catkin_tools.make_jobserver import jobserver_running_jobs
from catkin_tools.make_jobserver import jobserver_supported

from .common import get_build_type

from .color import clr

from .executor import Executor
from .executor import ExecutorEvent

from .job import CatkinJob
from .job import CMakeJob

from .output import OutputController


BUILDSPACE_MARKER_FILE = '.catkin_tools.yaml'
DEVELSPACE_MARKER_FILE = '.catkin_tools.yaml'


def get_ready_packages(packages, running_jobs, completed, failed=[]):
    """Returns packages which have no pending depends and are ready to be built

    Iterates through the packages, seeing if any of the packages which
    are not currently in running_jobs and are not in completed jobs, have all of
    their build and buildtool depends met, and are there for ready to be queued
    up and built.

    :param packages: topologically ordered packages in the workspace
    :type packages: dict
    :param running_jobs: currently running jobs which are building packages
    :type running_jobs: dict
    :param completed: list of packages in the workspace which have been built
    :type completed: list
    :param failed: list of packages in the workspace which failed to build
    :type failed: list
    :returns: list of package_path, package tuples which should be built
    :rtype: list
    """
    ready_packages = []
    workspace_packages = [(path, pkg) for path, pkg in packages]
    for path, package in packages:
        if package.name in (list(running_jobs.keys()) + completed + failed):
            continue
        # Collect build and buildtool depends, plus recursive build, buildtool, and run depends,
        # Excluding depends which are not in the workspace or which are completed
        uncompleted_depends = []
        depends = get_cached_recursive_build_depends_in_workspace(package, workspace_packages)
        for dep_pth, dep in depends:
            if dep.name not in completed:
                uncompleted_depends.append(dep)
        # If there are no uncompleted dependencies, add this package to the queue
        if not uncompleted_depends:
            ready_packages.append((path, package))
    # Return the new ready_packages
    return ready_packages


def queue_ready_packages(ready_packages, running_jobs, job_queue, context, force_cmake):
    """Adds packages which are ready to be built to the job queue

    :param ready_packages: packages which are ready to be built
    :type ready_packages: list
    :param running_jobs: jobs for building packages which are currently running
    :type running_jobs: dict
    :param job_queue: queue to put new jobs in, which will be consumed by executors
    :type job_queue: :py:class:`multiprocessing.Queue`
    :param context: context of the build environment
    :type context: :py:class:`catkin_tools.verbs.catkin_build.context.Context`
    :param force_cmake: must run cmake if True
    :type force_cmake: bool
    :returns: updated running_jobs dict
    :rtype: dict
    """
    for path, package in ready_packages:
        build_type = get_build_type(package)
        if build_type == 'catkin':
            job = CatkinJob(package, path, context, force_cmake)
        elif build_type == 'cmake':
            job = CMakeJob(package, path, context, force_cmake)
        running_jobs[package.name] = {
            'package_number': None,
            'job': job,
            'start_time': None
        }
        job_queue.put(job)
    return running_jobs


def determine_packages_to_be_built(packages, context):
    """Returns list of packages which should be built, and those package's deps.

    :param packages: list of packages to be built, if None all packages are built
    :type packages: list
    :param context: Workspace context
    :type context: :py:class:`catkin_tools.verbs.catkin_build.context.Context`
    :returns: tuple of packages to be built and those package's deps
    :rtype: tuple
    """
    start = time.time()

    # Get all the packages in the context source space
    # Suppress warnings since this is a utility function
    workspace_packages = find_packages(context.source_space_abs, exclude_subspaces=True, warnings=[])

    # If there are no packages raise
    if not workspace_packages:
        sys.exit("No packages were found in the source space '{0}'".format(context.source_space_abs))
    log("Found '{0}' packages in {1}."
        .format(len(workspace_packages), format_time_delta(time.time() - start)))

    # Order the packages by topology
    ordered_packages = topological_order_packages(workspace_packages)
    # Set the packages in the workspace for the context
    context.packages = ordered_packages
    # Determin the packages which should be built
    packages_to_be_built = []
    packages_to_be_built_deps = []

    # Determine the packages to be built
    if packages:
        # First assert all of the packages given are in the workspace
        workspace_package_names = dict([(pkg.name, (path, pkg)) for path, pkg in ordered_packages])
        for package in packages:
            if package not in workspace_package_names:
                sys.exit("Given package '{0}' is not in the workspace".format(package))
            # If metapackage, include run depends which are in the workspace
            package_obj = workspace_package_names[package][1]
            if 'metapackage' in [e.tagname for e in package_obj.exports]:
                for rdep in package_obj.run_depends:
                    if rdep.name in workspace_package_names:
                        packages.append(rdep.name)
        # Limit the packages to be built to just the provided packages
        for pkg_path, package in ordered_packages:
            if package.name in packages:
                packages_to_be_built.append((pkg_path, package))
                # Get the recursive dependencies for each of these packages
                pkg_deps = get_cached_recursive_build_depends_in_workspace(package, ordered_packages)
                packages_to_be_built_deps.extend(pkg_deps)
    else:
        # Only use whitelist when no other packages are specified
        if len(context.whitelist) > 0:
            packages_to_be_built = [p for p in ordered_packages if (p[1].name in context.whitelist)]
        else:
            packages_to_be_built = ordered_packages

    # Filter packages with blacklist
    if len(context.blacklist) > 0:
        packages_to_be_built = [
            (path, pkg) for path, pkg in packages_to_be_built
            if (pkg.name not in context.blacklist or pkg.name in packages)]
        packages_to_be_built_deps = [
            (path, pkg) for path, pkg in packages_to_be_built_deps
            if (pkg.name not in context.blacklist or pkg.name in packages)]
        ordered_packages = ordered_packages

    return packages_to_be_built, packages_to_be_built_deps, ordered_packages


def _create_unmerged_devel_setup(context):
    # Find all of the leaf packages in the workspace
    # where leaf means that nothing in the workspace depends on it

    # Find all packages in the source space
    # Suppress warnings since this is an internal function whose goal is not to
    # give feedback on the user's packages
    workspace_packages = find_packages(context.source_space_abs, exclude_subspaces=True, warnings=[])

    ordered_packages = topological_order_packages(workspace_packages)
    workspace_packages = dict([(p.name, p) for pth, p in workspace_packages.items()])
    dependencies = set([])
    for name, pkg in workspace_packages.items():
        dependencies.update([d.name for d in pkg.buildtool_depends + pkg.build_depends + pkg.run_depends])
    leaf_packages = []
    for name, pkg in workspace_packages.items():
        if pkg.name not in dependencies:
            leaf_packages.append(pkg.name)
    assert leaf_packages, leaf_packages  # Defensive, there should always be at least one leaf
    leaf_sources = []
    for pkg_name in leaf_packages:
        source_path = os.path.join(context.devel_space_abs, pkg_name, 'setup.sh')
        if os.path.isfile(source_path):
            leaf_sources.append('. {0}'.format(source_path))
    # In addition to the leaf packages, we need to source the recursive run depends of the leaf packages
    run_depends = get_recursive_run_depends_in_workspace(
        [workspace_packages[p] for p in leaf_packages], ordered_packages)
    run_depends_sources = []
    for run_dep_name in [p.name for pth, p in run_depends]:
        source_path = os.path.join(context.devel_space_abs, run_dep_name, 'setup.sh')
        if os.path.isfile(source_path):
            run_depends_sources.append('. {0}'.format(source_path))
    # Create the setup.sh file
    setup_sh_path = os.path.join(context.devel_space_abs, 'setup.sh')
    env_file = """\
#!/usr/bin/env sh
# generated from within catkin_tools/verbs/catkin_build/build.py

# This file is aggregates the many setup.sh files in the various
# unmerged devel spaces in this folder.
# This is occomplished by sourcing each leaf package and all the
# recursive run dependencies of those leaf packages

# Source the first package's setup.sh without the --extend option
{first_source}

# remove all passed in args, resetting $@, $*, $#, $n
shift $#
# set the --extend arg for rest of the packages setup.sh's
set -- $@ "--extend"
# source setup.sh for each of the leaf packages in the workspace
{leaf_sources}

# And now the setup.sh for each of their recursive run dependencies
{run_depends_sources}
""".format(
        first_source=leaf_sources[0],
        leaf_sources='\n'.join(leaf_sources[1:]),
        run_depends_sources='\n'.join(run_depends_sources)
    )
    with open(setup_sh_path, 'w') as f:
        f.write(env_file)
    # Make this file executable
    os.chmod(setup_sh_path, stat.S_IXUSR | stat.S_IWUSR | stat.S_IRUSR)
    # Create the setup.bash file
    setup_bash_path = os.path.join(context.devel_space_abs, 'setup.bash')
    with open(setup_bash_path, 'w') as f:
        f.write("""\
#!/usr/bin/env bash
# generated from within catkin_tools/verbs/catkin_build/build.py

CATKIN_SHELL=bash

# source setup.sh from same directory as this file
_BUILD_SETUP_DIR=$(builtin cd "`dirname "${BASH_SOURCE[0]}"`" && pwd)
. "$_BUILD_SETUP_DIR/setup.sh"
""")
    # Make this file executable
    os.chmod(setup_bash_path, stat.S_IXUSR | stat.S_IWUSR | stat.S_IRUSR)
    setup_zsh_path = os.path.join(context.devel_space_abs, 'setup.zsh')
    with open(setup_zsh_path, 'w') as f:
        f.write("""\
#!/usr/bin/env zsh
# generated from within catkin_tools/verbs/catkin_build/build.py

CATKIN_SHELL=zsh

# source setup.sh from same directory as this file
_BUILD_SETUP_DIR=$(builtin cd -q "`dirname "$0"`" && pwd)
emulate sh # emulate POSIX
. "$_BUILD_SETUP_DIR/setup.sh"
emulate zsh # back to zsh mode
""")
    # Make this file executable
    os.chmod(setup_zsh_path, stat.S_IXUSR | stat.S_IWUSR | stat.S_IRUSR)


def _create_unmerged_devel_setup_for_install(context):
    for path in [os.path.join(context.devel_space_abs, f) for f in ['setup.sh', 'setup.bash', 'setup.zsh']]:
        with open(path, 'w') as f:
            f.write("""\
#!/usr/bin/env sh
# generated from within catkin_tools/verbs/catkin_build/build.py

echo "Error: This workspace was built with the '--install' option."
echo "       You should source the setup files in the install space instead."
echo "       Your environment has not been changed."
""")


def verify_start_with_option(start_with, packages, all_packages, packages_to_be_built):
    if start_with is not None:
        if start_with not in [pkg.name for pth, pkg in all_packages]:
            sys.exit("Package given for --start-with, '{0}', is not in the workspace.".format(start_with))
        elif start_with not in [pkg.name for pth, pkg in packages_to_be_built]:
            sys.exit("Package given for --start-with, '{0}', "
                     "is in the workspace but would not be built with given package arguments: '{1}'"
                     .format(start_with, ' '.join(packages)))


def print_error_summary(errors, no_notify, log_dir):
    wide_log(clr("[build] There were '" + str(len(errors)) + "' @!@{rf}errors@|:"))
    if not no_notify:
        notify("Build Failed", "there were {0} errors".format(len(errors)))
    for error in errors:
        if error.event_type == 'exit':
            wide_log("""\
Executor '{exec_id}' had an unhandled exception while processing package '{package}':

{data[exc]}""".format(exec_id=error.executor_id + 1, **error.__dict__))
        else:
            wide_log(clr("""
@{rf}Failed@| to build package '@{cf}{package}@|' because the following command:

@!@{kf}# Command to reproduce:@|
cd {location} && {cmd.cmd_str}; cd -

@!@{kf}# Path to log:@|
cat {log_dir}

@{rf}Exited@| with return code: @!{retcode}@|""").format(package=error.package,
                                                         log_dir=os.path.join(log_dir, error.package + '.log'),
                                                         **error.data))


def print_items_in_columns(items_in, number_of_columns):
    number_of_items_in_line = 0
    line_template = "{}" * number_of_columns
    line_items = []
    items = list(items_in)
    while items:
        line_items.append(items.pop(0))
        number_of_items_in_line += 1
        if number_of_items_in_line == number_of_columns:
            wide_log(line_template.format(*line_items))
            line_items = []
            number_of_items_in_line = 0
    if line_items:
        wide_log(("{}" * len(line_items)).format(*line_items))


def print_build_summary(context, packages_to_be_built, completed_packages, failed_packages):
    # Calculate the longest package name
    max_name_len = max([len(pkg.name) for _, pkg in context.packages])

    def get_template(template_name, column_width):
        templates = {
            'successful': " @!@{gf}Successful@| @{cf}{package:<" + str(column_width) + "}@|",
            'failed': " @!@{rf}Failed@|     @{cf}{package:<" + str(column_width) + "}@|",
            'not_built': " @!@{kf}Not built@|  @{cf}{package:<" + str(column_width) + "}@|",
        }
        return templates[template_name]

    # Setup templates for comparison
    successful_template = get_template('successful', max_name_len)
    failed_template = get_template('failed', max_name_len)
    not_built_template = get_template('not_built', max_name_len)
    # Calculate the maximum _printed_ length for each template
    faux_package_name = ("x" * max_name_len)
    templates = [
        remove_ansi_escape(clr(successful_template).format(package=faux_package_name)),
        remove_ansi_escape(clr(failed_template).format(package=faux_package_name)),
        remove_ansi_escape(clr(not_built_template).format(package=faux_package_name)),
    ]
    # Calculate the longest column using the longest template
    max_column_len = max([len(template) for template in templates])
    # Calculate the number of columns
    number_of_columns = (terminal_width() / max_column_len) or 1

    successfuls = {}
    faileds = {}
    not_builts = {}
    non_whitelisted = {}
    blacklisted = {}

    for (_, pkg) in context.packages:
        if pkg.name in context.blacklist:
            blacklisted[pkg.name] = clr(not_built_template).format(package=pkg.name)
        elif len(context.whitelist) > 0 and pkg.name not in context.whitelist:
            non_whitelisted[pkg.name] = clr(not_built_template).format(package=pkg.name)
        elif pkg.name in completed_packages:
            successfuls[pkg.name] = clr(successful_template).format(package=pkg.name)
        else:
            if pkg.name in failed_packages:
                faileds[pkg.name] = clr(failed_template).format(package=pkg.name)
            else:
                not_builts[pkg.name] = clr(not_built_template).format(package=pkg.name)

    # Combine successfuls and not_builts, sort by key, only take values
    wide_log("")
    wide_log("Build summary:")
    combined = dict(successfuls)
    combined.update(not_builts)
    non_failed = [v for k, v in sorted(combined.items(), key=operator.itemgetter(0))]
    print_items_in_columns(non_failed, number_of_columns)

    # Print out whitelisted packages
    if len(non_whitelisted) > 0:
        wide_log("")
        wide_log("Non-Whitelisted Packages:")
        non_whitelisted_list = [v for k, v in sorted(non_whitelisted.items(), key=operator.itemgetter(0))]
        print_items_in_columns(non_whitelisted_list, number_of_columns)

    # Print out blacklisted packages
    if len(blacklisted) > 0:
        wide_log("")
        wide_log("Blacklisted Packages:")
        blacklisted_list = [v for k, v in sorted(blacklisted.items(), key=operator.itemgetter(0))]
        print_items_in_columns(blacklisted_list, number_of_columns)

    # Faileds only, sort by key, only take values
    failed = [v for k, v in sorted(faileds.items(), key=operator.itemgetter(0))]
    if len(failed) > 0:
        wide_log("")
        wide_log("Failed packages:")
        print_items_in_columns(failed, number_of_columns)
    else:
        wide_log("")
        wide_log("All packages built successfully.")

    wide_log("")
    wide_log(clr("[build] @!@{gf}Successfully@| built '@!@{cf}{0}@|' packages, "
                 "@!@{rf}failed@| to build '@!@{cf}{1}@|' packages, "
                 "and @!@{kf}did not try to build@| '@!@{cf}{2}@|' packages.").format(
        len(successfuls), len(faileds), len(not_builts)
    ))


def build_isolated_workspace(
    context,
    packages=None,
    start_with=None,
    no_deps=False,
    jobs=None,
    force_cmake=False,
    force_color=False,
    quiet=False,
    interleave_output=False,
    no_status=False,
    limit_status_rate=0.0,
    lock_install=False,
    no_notify=False,
    continue_on_failure=False,
    summarize_build=None,
):
    """Builds a catkin workspace in isolation

    This function will find all of the packages in the source space, start some
    executors, feed them packages to build based on dependencies and topological
    ordering, and then monitor the output of the executors, handling loggings of
    the builds, starting builds, failing builds, and finishing builds of
    packages, and handling the shutdown of the executors when appropriate.

    :param context: context in which to build the catkin workspace
    :type context: :py:class:`catkin_tools.verbs.catkin_build.context.Context`
    :param packages: list of packages to build, by default their dependencies will also be built
    :type packages: list
    :param start_with: package to start with, skipping all packages which proceed it in the topological order
    :type start_with: str
    :param no_deps: If True, the dependencies of packages will not be built first
    :type no_deps: bool
    :param jobs: number of parallel package build jobs
    :type jobs: int
    :param force_cmake: forces invocation of CMake if True, default is False
    :type force_cmake: bool
    :param force_color: forces colored output even if terminal does not support it
    :type force_color: bool
    :param quiet: suppresses the output of commands unless there is an error
    :type quiet: bool
    :param interleave_output: prints the output of commands as they are received
    :type interleave_output: bool
    :param no_status: disables status bar
    :type no_status: bool
    :param limit_status_rate: rate to which status updates are limited; the default 0, places no limit.
    :type limit_status_rate: float
    :param lock_install: causes executors to synchronize on access of install commands
    :type lock_install: bool
    :param no_notify: suppresses system notifications
    :type no_notify: bool
    :param continue_on_failure: do not stop building other jobs on error
    :type continue_on_failure: bool
    :param summarize_build: if True summarizes the build at the end, if None and continue_on_failure is True and the
        the build fails, then the build will be summarized, but if False it never will be summarized.
    :type summarize_build: bool

    :raises: SystemExit if buildspace is a file or no packages were found in the source space
        or if the provided options are invalid
    """
    # Assert that the limit_status_rate is valid
    if limit_status_rate < 0:
        sys.exit("The value of --status-rate must be greater than or equal to zero.")
    # If no_deps is given, ensure packages to build are provided
    if no_deps and packages is None:
        sys.exit("With --no-deps, you must specify packages to build.")
    # Make sure there is a build folder and it is not a file
    if os.path.exists(context.build_space_abs):
        if os.path.isfile(context.build_space_abs):
            sys.exit(clr(
                "@{rf}Error:@| Build space '{0}' exists but is a file and not a folder."
                .format(context.build_space_abs)))
    # If it dosen't exist, create it
    else:
        log("Creating build space directory, '{0}'".format(context.build_space_abs))
        os.makedirs(context.build_space_abs)

    # Check for catkin_make droppings
    if context.corrupted_by_catkin_make():
        sys.exit(
            clr("@{rf}Error:@| Build space `{0}` exists but appears to have previously been "
                "created by the `catkin_make` or `catkin_make_isolated` tool. "
                "Please choose a different directory to use with `catkin build` "
                "or clean the build space.").format(context.build_space_abs))

    # Declare a buildspace marker describing the build config for error checking
    buildspace_marker_data = {
        'workspace': context.workspace,
        'profile': context.profile,
        'install': context.install,
        'install_space': context.install_space_abs,
        'devel_space': context.devel_space_abs,
        'source_space': context.source_space_abs}

    # Check build config
    if os.path.exists(os.path.join(context.build_space_abs, BUILDSPACE_MARKER_FILE)):
        with open(os.path.join(context.build_space_abs, BUILDSPACE_MARKER_FILE)) as buildspace_marker_file:
            existing_buildspace_marker_data = yaml.load(buildspace_marker_file)
            misconfig_lines = ''
            for (k, v) in existing_buildspace_marker_data.items():
                new_v = buildspace_marker_data.get(k, None)
                if new_v != v:
                    misconfig_lines += (
                        '\n - %s: %s (stored) is not %s (commanded)' %
                        (k, v, new_v))
            if len(misconfig_lines) > 0:
                sys.exit(clr(
                    "\n@{rf}Error:@| Attempting to build a catkin workspace using build space: "
                    "\"%s\" but that build space's most recent configuration "
                    "differs from the commanded one in ways which will cause "
                    "problems. Fix the following options or use @{yf}`catkin "
                    "clean -b`@| to remove the build space: %s" %
                    (context.build_space_abs, misconfig_lines)))

    # Write the current build config for config error checking
    with open(os.path.join(context.build_space_abs, BUILDSPACE_MARKER_FILE), 'w') as buildspace_marker_file:
        buildspace_marker_file.write(yaml.dump(buildspace_marker_data, default_flow_style=False))

    # Summarize the context
    summary_notes = []
    if force_cmake:
        summary_notes += [clr("@!@{cf}NOTE:@| Forcing CMake to run for each package.")]
    log(context.summary(summary_notes))

    # Find list of packages in the workspace
    packages_to_be_built, packages_to_be_built_deps, all_packages = determine_packages_to_be_built(packages, context)
    completed_packages = []
    if no_deps:
        # Consider deps as "completed"
        completed_packages.extend(packages_to_be_built_deps)
    else:
        # Extend packages to be built to include their deps
        packages_to_be_built.extend(packages_to_be_built_deps)
    # Also resort
    packages_to_be_built = topological_order_packages(dict(packages_to_be_built))
    # Check the number of packages to be built
    if len(packages_to_be_built) == 0:
        log(clr('[build] No packages to be built.'))
        return

    max_package_name_length = max([len(pkg.name) for pth, pkg in packages_to_be_built]) if packages_to_be_built else 0
    # Assert start_with package is in the workspace
    verify_start_with_option(start_with, packages, all_packages, packages_to_be_built + packages_to_be_built_deps)

    # Setup pool of executors
    executors = {}
    # The communication queue can have ExecutorEvent's or str's passed into it from the executors
    comm_queue = Queue()
    # The job queue has Jobs put into it
    job_queue = Queue()
    # Lock for install space
    install_lock = Lock() if lock_install else FakeLock()
    # Determine the number of executors
    try:
        if jobs:
            jobs = int(jobs)
            if jobs < 1:
                sys.exit("Specified number of jobs '{0}' is not positive.".format(jobs))
    except ValueError:
        sys.exit("Specified number of jobs '{0}' is no integer.".format(jobs))
    try:
        jobs = cpu_count() if jobs is None else jobs
    except NotImplementedError:
        log('Failed to determine the cpu_count, falling back to 1 jobs as the default.')
        jobs = 1 if jobs is None else jobs
    # If only one set of jobs, turn on interleaving to get more responsive feedback
    if jobs == 1:
        # TODO: make the system more intelligent so that it can automatically switch to streaming output
        #       when only one job is building, even if multiple jobs could be building
        quiet = False
        interleave_output = True
    # Start the executors
    for x in range(jobs):
        e = Executor(x, context, comm_queue, job_queue, install_lock, continue_on_failure)
        executors[x] = e
        e.start()

    try:  # Finally close out now running executors
        # Variables for tracking running jobs and built/building packages
        start = time.time()
        total_packages = len(packages_to_be_built)
        package_count = 0
        running_jobs = {}
        last_status_update_time = time.time()
        limit_status_period = (1.0 / limit_status_rate) if limit_status_rate else 0
        log_dir = os.path.join(context.build_space_abs, 'build_logs')
        color = True
        if not force_color and not is_tty(sys.stdout):
            color = False
        out = OutputController(log_dir, quiet, interleave_output,
                               color, max_package_name_length, prefix_output=(jobs > 1))
        if no_status:
            disable_wide_log()

        # Prime the job_queue
        ready_packages = []
        failed_packages = []
        if start_with is None:
            ready_packages = get_ready_packages(packages_to_be_built, running_jobs, completed_packages)
        while start_with is not None:
            ready_packages.extend(get_ready_packages(packages_to_be_built, running_jobs, completed_packages))
            while ready_packages:
                pth, pkg = ready_packages.pop(0)
                if pkg.name != start_with:
                    completed_packages.append(pkg.name)
                    package_count += 1
                    wide_log("[build] Skipping package '{0}'".format(pkg.name))
                else:
                    ready_packages.insert(0, (pth, pkg))
                    start_with = None
                    break
        running_jobs = queue_ready_packages(ready_packages, running_jobs, job_queue, context, force_cmake)
        assert running_jobs

        error_state = False
        errors = []

        def set_error_state(error_state):
            if error_state:
                return
            # Set the error state to prevent new jobs
            error_state = True
            # Empty the job queue
            while not job_queue.empty():
                job_queue.get()
            # Kill the executors by sending a None to the job queue for each of them
            for x in range(len(executors)):
                job_queue.put(None)
                if x in executors:
                    executors[x].should_shutdown = True

        # While any executors are running, process executor events
        while executors:
            try:
                # Try to get an event from the communications queue
                try:
                    event = comm_queue.get(True, 0.1)
                except Empty:
                    # timeout occured, create null event to pass through checks
                    event = ExecutorEvent(None, None, None, None)

                if event.event_type == 'job_started':
                    package_count += 1
                    running_jobs[event.package]['package_number'] = package_count
                    running_jobs[event.package]['start_time'] = time.time()
                    out.job_started(event.package)

                if event.event_type == 'command_started':
                    out.command_started(event.package, event.data['cmd'], event.data['location'])

                if event.event_type == 'command_log':
                    out.command_log(event.package, event.data['message'])

                if event.event_type == 'command_failed':
                    out.command_failed(event.package, event.data['cmd'], event.data['location'], event.data['retcode'])
                    # Add to list of errors
                    errors.append(event)

                if event.event_type == 'command_finished':
                    out.command_finished(event.package, event.data['cmd'],
                                         event.data['location'], event.data['retcode'])

                if event.event_type == 'job_finished':
                    completed_packages.append(event.package)
                    run_time = format_time_delta(time.time() - running_jobs[event.package]['start_time'])
                    out.job_finished(event.package, run_time)
                    del running_jobs[event.package]
                    # If shutting down, do not add new packages
                    if error_state:
                        continue
                    # Calculate new packages
                    if not no_status:
                        wide_log('[build] Calculating new jobs...', end='\r')
                        sys.stdout.flush()
                    ready_packages = get_ready_packages(packages_to_be_built, running_jobs, completed_packages,
                                                        failed_packages)
                    running_jobs = queue_ready_packages(ready_packages, running_jobs, job_queue, context, force_cmake)
                    # Make sure there are jobs to be/being processed, otherwise kill the executors
                    if not running_jobs:
                        # Kill the executors by sending a None to the job queue for each of them
                        for x in range(jobs):
                            job_queue.put(None)

                if event.event_type == 'job_failed':
                    failed_packages.append(event.package)
                    run_time = format_time_delta(time.time() - running_jobs[event.package]['start_time'])
                    out.job_failed(event.package, run_time)
                    del running_jobs[event.package]
                    # if the continue_on_failure option was not given, stop the executors
                    if not continue_on_failure:
                        set_error_state(error_state)
                    # If shutting down, do not add new packages
                    if error_state:
                        continue
                    # Calculate new packages
                    if not no_status:
                        wide_log('[build] Calculating new jobs...', end='\r')
                        sys.stdout.flush()
                    ready_packages = get_ready_packages(packages_to_be_built, running_jobs, completed_packages,
                                                        failed_packages)
                    running_jobs = queue_ready_packages(ready_packages, running_jobs, job_queue, context, force_cmake)
                    # Make sure there are jobs to be/being processed, otherwise kill the executors
                    if not running_jobs:
                        # Kill the executors by sending a None to the job queue for each of them
                        for x in range(jobs):
                            job_queue.put(None)

                # If an executor exit event, join it and remove it from the executors list
                if event.event_type == 'exit':
                    # If an executor has an exception, set the error state
                    if event.data['reason'] == 'exception':
                        set_error_state(error_state)
                        errors.append(event)
                    # Join and remove it
                    executors[event.executor_id].join()
                    del executors[event.executor_id]

                if not no_status:
                    # Update the status bar on the screen
                    executing_jobs = []
                    for name, value in running_jobs.items():
                        number, start_time = value['package_number'], value['start_time']
                        if number is None or start_time is None:
                            continue
                        executing_jobs.append({
                            'number': number,
                            'name': name,
                            'run_time': format_time_delta_short(time.time() - start_time)
                        })
                    msg = clr("[build - {run_time}] ").format(run_time=format_time_delta_short(time.time() - start))
                    # If errors post those
                    if errors:
                        for error in errors:
                            msg += clr("[!{package}] ").format(package=error.package)
                    # Print them in order of started number
                    for job_msg_args in sorted(executing_jobs, key=lambda args: args['number']):
                        msg += clr("[{name} - {run_time}] ").format(**job_msg_args)

                    if jobserver_supported():
                        msg_rhs = clr("[{0}/{1} Jobs | {2}/{3} Active | {4}/{5} Completed]").format(
                            jobserver_running_jobs(),
                            jobserver_max_jobs(),
                            len(executing_jobs),
                            len(executors),
                            len(packages) if no_deps else len(completed_packages),
                            total_packages
                        )
                    else:
                        msg_rhs = clr("[{0}/{1} Active | {2}/{3} Completed]").format(
                            len(executing_jobs),
                            len(executors),
                            len(packages) if no_deps else len(completed_packages),
                            total_packages
                        )

                    # Update title bar
                    sys.stdout.write("\x1b]2;[build] {0}/{1}\x07".format(
                        len(packages) if no_deps else len(completed_packages),
                        total_packages
                    ))
                    # Update status bar
                    # If the status_rate is zero, always do the status update
                    do_status_update = (limit_status_rate == 0)
                    # Otherwise calculate the time delta
                    if not do_status_update:
                        if (time.time() - last_status_update_time) >= limit_status_period:
                            last_status_update_time = time.time()
                            do_status_update = True
                    # Conditionally do the status update
                    if do_status_update:
                        wide_log(msg, rhs=msg_rhs, end='\r')
                        sys.stdout.flush()
            except (KeyboardInterrupt, EOFError):
                wide_log("[build] User interrupted, stopping.")
                set_error_state(error_state)
        # All executors have shutdown
        sys.stdout.write("\x1b]2;\x07")
        if not errors:
            if context.isolate_devel:
                if not context.install:
                    _create_unmerged_devel_setup(context)
                else:
                    _create_unmerged_devel_setup_for_install(context)
            if summarize_build:
                print_build_summary(context, packages_to_be_built, completed_packages, failed_packages)
            wide_log("[build] Finished.")
            if not no_notify:
                notify("Build Finished", "{0} packages built".format(total_packages))
            return 0
        # Else, handle errors
        print_error_summary(errors, no_notify, log_dir)
        wide_log("")
        if summarize_build is True or summarize_build is not False and continue_on_failure is True:
            # Always print summary if summarize_build is True
            # Conditionally add summary on errors if summarize_build is not explicitly False and
            # continue_on_failure is True.
            print_build_summary(context, packages_to_be_built, completed_packages, failed_packages)
        sys.exit(1)
    finally:
        # Ensure executors go down
        for x in range(jobs):
            job_queue.put(None)
