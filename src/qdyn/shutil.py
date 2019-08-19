import contextlib
import os
from os import rmdir
from shutil import *


def mkdir(name, mode=0o750):
    """
    Implementation of 'mkdir -p': Creates folder with the given `name` and the
    given permissions (`mode`)

    * Create missing parents folder
    * Do nothing if the folder with the given `name` already exists
    * Raise OSError if there is already a file with the given `name`
    """
    if os.path.isdir(name):
        pass
    elif os.path.isfile(name):
        raise OSError(
            "A file with the same name as the desired "
            "dir, '%s', already exists." % name
        )
    else:
        os.makedirs(name, mode)


def touch(fname):
    """
    Touch a filename (similar to the unix 'touch' utility). If the file does
    not exist already, create it. Otherwise, update its access time.
    """
    with open(fname, 'a'):
        os.utime(fname, None)


def find_files(directory, pattern):
    """
    Iterate (recursively) over all the files matching the shell pattern
    ('*' will yield all files) in the given directory. There is no guarantee on
    the order in which files are processed

    >>> files = ["find_files_test/a.txt", "find_files_test/a.dat",
    ...          "find_files_test/sub/b.txt", "find_files_test/sub/c.txt"]
    >>> mkdir("find_files_test/sub")
    >>> for file in files:
    ...     touch(file)
    >>> found_files = set()
    >>> for file in find_files("find_files_test", '*.txt'):
    ...     found_files.add(file)
    >>> for file in files:
    ...     if file.endswith('txt'):
    ...         assert file in found_files
    ...     else:
    ...         assert file not in found_files
    >>> rmtree("find_files_test")
    """
    import fnmatch

    if not os.path.isdir(directory):
        raise IOError("directory %s does not exist" % directory)
    for root, _, files in os.walk(directory):
        for basename in files:
            if fnmatch.fnmatch(basename, pattern):
                filename = os.path.join(root, basename)
                yield filename


# 'chdir' context manager
@contextlib.contextmanager
def chdir(dirname=None):
    """Change directory.

    Use as::

        >>> mkdir('dir')
        >>> with chdir('dir'):
        ...     pass
        >>> rmdir('dir')
    """
    curdir = os.getcwd()
    try:
        if dirname is not None:
            os.chdir(dirname)
        yield
    finally:
        os.chdir(curdir)


def tail(file, n):
    """Print the last n lines of the given file"""
    with open(file) as in_fh:
        lines = in_fh.readlines()
        print("".join(lines[-n:]))
