#!/usr/bin/env python3
"""
This program is used for deploying code modules onto the NCI.

It is configured by a YAML file, which specifies:
 - variables
 - files to copy with permissions
 - template files with their destination (eg, modulefiles)
 - (opt) Conda environment to create
 - (opt) Pip style requirements.txt to install to a directory

It requires python 3.7+ and pyyaml.
Use a qsub interactive copyq job on raijin with sufficient memory to run the following commands at the NCI:
New DEA-Env Module
  $ module use /g/data/v10/public/modules/modulefiles/
  $ module load python3/3.7.4
  # if pyyaml is not installed in gadi
  $ pip install PyYAML --user

  $ # Building a new Environment Module:
  $ python3 build_environment_module.py dea-env/modulespec.yaml

New DEA Module
  $ module use /g/data/v10/public/modules/modulefiles/
  $ module load python3/3.7.2

  $ # Building a new DEA Module
  $ python3 build_environment_module.py dea/modulespec.yaml

It used to be able to perform a miniconda installation, but that turned out to
be flaky, so we now maintain a central miniconda install, and create environments
as an where required. With the added benefit of keeping a central cache of
packages.
"""

import datetime
import logging
import os
import shutil
import string
import subprocess
import sys
from pathlib import Path

import yaml

MODULE_DIR = "/g/data/v10/public/modules"

LOG_NAME = "build_dea_module.log"
FILE_HANDLER = logging.FileHandler(filename=LOG_NAME, mode="w", encoding="utf-8")
STDOUT_HANDLER = logging.StreamHandler(sys.stdout)
HANDLERS = [FILE_HANDLER, STDOUT_HANDLER]

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] {%(filename)30s:%(lineno)3d} %(levelname)s: %(message)s",
    handlers=HANDLERS,
)

LOG = logging.getLogger("build-dea-module")


def pre_check(config):
    """
    Perform pre-checks before creating a new module

    :param config: Configuration parameters
    :return: None
    """
    LOG.info("Performing pre-check before installing module")
    if "PYTHONPATH" in os.environ:
        raise Exception(
            "The PYTHONPATH environment variable must NOT be set when creating modules."
        )

    module_path = Path(config["variables"]["module_path"])
    if module_path.exists():
        raise Exception(
            f"The destination path {module_path} already exists, "
            f"please remove it and try again."
        )


def prep(config_path):
    """
    Prepare environment variables before creating a new module

    :param config_path: Configuration Path
    :return: None
    """
    LOG.info("Preparing environment variables")
    # Write files as group and world readable
    os.umask(0o22)
    os.chdir(config_path.parent)
    os.environ["LC_ALL"] = "en_AU.utf8"
    os.environ["LANG"] = "C.UTF-8"

    # make sure no ~/.local stuff interferes with the install
    os.environ["PYTHONNOUSERSITE"] = "true"


def date(date_format="%Y%m%d") -> str:
    """
    Return datatime format as YYYYMMDD

    :param date_format: Date format as a str
    :return: datetime as a str
    """
    return datetime.datetime.now().strftime(date_format)


def _log_output(line):
    try:
        LOG.info(line)
    except UnicodeEncodeError:
        LOG.warning("UnicodeEncodeError: %s", line.encode("ascii", "replace"))


def run_command(cmd):
    """
    Run subprocess command and print the output on the terminal and the log file

    :param cmd: Command to execute
    :return: None
    """
    LOG.info("Running command: %s", cmd)
    return subprocess.run(
        cmd,
        shell=True,
        check=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def install_conda_packages(env_file, variables):
    """
    Install required conda packages specified in environment yaml file

    :param env_file: Environment yaml file path
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :return: None
    """
    LOG.info("Installing conda packages from %s", env_file)

    conda_path = variables["conda_path"]
    module_path = variables["module_path"]

    run_command(f"{conda_path} env create -p {module_path} -v --file {env_file}")


def write_template(template_file, variables, output_file):
    """
    Create a new dea module file using template file

    :param template_file: Module template file
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :param output_file: New dea module file
    :return: None
    """
    LOG.info("Filling template file %s to %s", template_file, output_file)
    LOG.info("Ensuring parent dir %s exists", output_file.parent)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    template_contents = template_file.read_text()
    template = string.Template(template_contents)
    output_file.write_text(template.substitute(variables))


def fill_templates_from_variables(template_dict, variables):
    """
    Fill templates from the configuration variables as per configuration settings in modulespec yaml file

    :param template_dict: Template dictionary to be updated
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :return:
    """
    for key, val in template_dict.items():
        template_dict[key] = val.format(**variables)


def copy_files(copy_tasks, variables):
    """
    Copy files from source to destination as per configuration settings in modulespec yaml file

    :param copy_tasks: Copy tasks
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :return:
    """
    for task in copy_tasks:
        fill_templates_from_variables(task, variables)
        src = Path(task["src"])
        dest = Path(task["dest"])

        LOG.info("Copying %s to %s", src, dest)
        LOG.info("Ensuring parent dir %s exists", dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy(src, dest)

        if "chmod" in task:
            perms = int(task["chmod"], base=8)
            LOG.info("Setting %s permissions to %s", dest, oct(perms))
            dest.chmod(perms)


def wget_files(wget_tasks, variables):
    """
    wget files from source to destination as per configuration settings in modulespec yaml file

    :param wget_tasks: Tasks to download files with `wget`
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :return:
    """
    for task in wget_tasks:
        fill_templates_from_variables(task, variables)
        src = Path(task["src"])
        dest = Path(task["dest"])

        LOG.info("Copying %s to %s", src, dest)
        LOG.info("Ensuring parent dir %s exists", dest.parent)
        dest.parent.mkdir(parents=True, exist_ok=True)

        run_command(f"wget -O {desc} {src}")

        if "chmod" in task:
            perms = int(task["chmod"], base=8)
            LOG.info("Setting %s permissions to %s", dest, oct(perms))
            dest.chmod(perms)


def read_config(path):
    """
    Read the configuration file and return a dictionary of configuration variables

    :param path: Configuration file path
    :return: Dictionary of configuration variables
    """
    return yaml.safe_load(path.read_text())


def copy_and_fill_templates(template_tasks, variables):
    """
    Copy and fill templates

    :param template_tasks: Template tasks
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :return: None
    """
    for task in template_tasks:
        fill_templates_from_variables(task, variables)

        src = Path(task["src"])
        dest = Path(task["dest"])
        LOG.info("Copy and fill dea-env modulefile %s in %s", src, dest)
        # Write the module file template to modulefiles/dea-env directory
        write_template(src, variables, dest)

        if "chmod" in task:
            perms = int(task["chmod"], base=8)
            LOG.info("Setting %s permissions to %s", dest, oct(perms))
            dest.chmod(perms)


def include_templated_vars(config):
    """
    Update template variables

    :param config: Dictionary of configuration variables
    :return: None
    """
    fill_templates_from_variables(config["templated_variables"], config["variables"])
    config["variables"].update(config["templated_variables"])

    del config["templated_variables"]


def fix_module_permissions(module_path):
    """
    Fix module permissions

    :param module_path: Module path
    :return: None
    """
    LOG.info('Setting module "%s" permission as world readable', module_path)
    run_command(f'chmod -R u+rx,go+rx,go-w "{module_path}"')


def install_pip_packages(pip_conf, variables):
    """
    Install pip packages

    Runs command ``pip_cmd``

    :param pip_conf: Dictionary of pip configurations
    :param variables: Configuration variables as per configuration settings in modulespec yaml file
    :return: None
    """
    fill_templates_from_variables(pip_conf, variables)
    pip_cmd = pip_conf["pip_cmd"]

    LOG.info(f'Running: "{pip_cmd}"')
    run_command(pip_cmd)


def find_default_version(module_name):
    """
    Find default version of the dea module

    :param module_name: Module name
    :return: Version on success else raise exception
    """
    cmd = f"module --terse avail {module_name}"
    LOG.info("Running command: %s", cmd)
    output = subprocess.run(
        cmd,
        shell=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        encoding="ascii",
    )
    versions = [
        version
        for version in output.stdout.splitlines()
        if f"{module_name}/" in version
    ]
    default_version = [version for version in versions if "(default)" in version]

    if default_version:
        ret_val = default_version[0].replace("(default)", "")
    elif len(versions) > 0:
        ret_val = versions[-1]
    else:
        raise Exception("No version of module %s is available." % module_name)
    return ret_val


def run_final_commands_on_module(commands, module_path):
    """
    Run final commands as per configurations in modulespec.yaml file

    :param commands: List of commands
    :param module_path: Module path
    :return: None
    """
    os.environ["PATH"] = f'{module_path}/bin:{os.environ["PATH"]}'
    for command in commands:
        cmd = f"{module_path}/bin/{command}"
        run_command(cmd)


def include_stable_module_dep_versions(config):
    """
    Include stable module dependency versions

    :param config: Dictionary of configuration variables
    :return: None
    """
    stable_module_deps = config.get("stable_module_deps", [])
    for dep in stable_module_deps:
        default_version = find_default_version(dep)
        dep = dep.replace("-", "_")
        config["variables"][f"fixed_{dep}"] = default_version


def main(config_path):
    """
    Build new environment module

    :param config_path: Configuration path
    :return: None
    """
    # To keep the migration consistency across platforms (macOS/Windows/Linux)
    ospath = r"%s" % os.getcwd().replace("\\", "/")

    # Setup Logging
    logging.basicConfig(level=logging.DEBUG)
    LOG.info("Reading config file")
    config = read_config(config_path)
    variables = config["variables"]

    if "module_version" not in variables:
        variables["module_version"] = date()
    include_templated_vars(config)
    include_stable_module_dep_versions(config)

    pre_check(config)
    prep(config_path)

    if "install_conda_packages" in config:
        install_conda_packages(config["install_conda_packages"], variables)

    if "install_pip_packages" in config:
        install_pip_packages(config["install_pip_packages"], variables)

    copy_files(config.get("copy_files", []), variables)
    wget_files(config.get("wget_files", []), variables)
    copy_and_fill_templates(config.get("template_files", []), variables)

    LOG.info("Run final commands on module")
    if "finalise_commands" in config and config["finalise_commands"]:
        run_final_commands_on_module(
            config["finalise_commands"], variables["module_path"]
        )

    fix_module_permissions(variables["module_path"])

    LOG.info("List installed python dependencies and their versions:")
    module = f'{variables["module_name"]}/{variables["module_version"]}'
    run_command(f"module load {module}; pip freeze")

    # Save the log file for posterity
    shutil.copy(ospath + "/" + LOG_NAME, variables["module_path"] + "/" + LOG_NAME)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
