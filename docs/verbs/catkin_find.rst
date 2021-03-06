``catkin locate`` -- Find Workspace Locations
===========================================

The ``locate`` verb can be used to locate important locations in the workspace such as
the active ``source``, ``build``, ``devel``, and ``install`` spaces, and package
directories in the workspace.

Full Command-Line Interface
^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: text

    usage: catkin locate [-h] [--workspace WORKSPACE] [--profile PROFILE] [-e] [-r]
                       [-s | -b | -d | -i]
                       [PACKAGE]

    Get the paths to various locations in a workspace.

    optional arguments:
      -h, --help            show this help message and exit
      --workspace WORKSPACE, -w WORKSPACE
                            The path to the catkin_tools workspace or a directory
                            contained within it (default: ".")
      --profile PROFILE     The name of a config profile to use (default: active
                            profile)

    Behavior:
      -e, --existing-only   Only print paths to existing directories.
      -r, --relative        Print relative paths instead of the absolute paths.

    Sub-Space Options:
      Get the absolute path to one of the following locations in the given
      workspace with the given profile.

      -s, --src             Get the path to the source space.
      -b, --build           Get the path to the build space.
      -d, --devel           Get the path to the devel space.
      -i, --install         Get the path to the install space.

    Package Directories:
      Get the absolute path to package directories in the given workspace and
      sub-space. By default this will output paths in the workspace's source
      space. If the -b (--build) flag is given, it will output the path to the
      package's build directory. If the -d or -i (--devel or --install) flags
      are given, it will output the path to the package's share directory in
      that space.

      PACKAGE               The name of a package to locate.
