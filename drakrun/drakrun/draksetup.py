import configparser
import hashlib
import logging
import io
import shlex
import sys
import os
import re
import json
import time
import secrets
import subprocess
import string
import tempfile
from shutil import copyfile
from typing import Optional

import click
import requests
from requests import RequestException
from minio import Minio
from minio.error import NoSuchKey
from drakrun.drakpdb import fetch_pdb, make_pdb_profile, dll_file_list, pdb_guid, DLL
from drakrun.config import InstallInfo, LIB_DIR, VOLUME_DIR, PROFILE_DIR, ETC_DIR, VM_CONFIG_DIR
from drakrun.networking import setup_vm_network, start_dnsmasq, delete_vm_network, stop_dnsmasq
from drakrun.storage import get_storage_backend, REGISTERED_BACKEND_NAMES
from drakrun.injector import Injector
from drakrun.vm import generate_vm_conf, FIRST_CDROM_DRIVE, SECOND_CDROM_DRIVE, get_all_vm_conf, delete_vm_conf, VirtualMachine
from drakrun.util import RuntimeInfo, VmiOffsets, safe_delete
from tqdm import tqdm
from pathlib import Path, PureWindowsPath
import traceback


conf = configparser.ConfigParser()
conf.read(os.path.join(ETC_DIR, "config.ini"))


def find_default_interface():
    routes = subprocess.check_output('ip route show default', shell=True, stderr=subprocess.STDOUT) \
        .decode('ascii').strip().split('\n')

    for route in routes:
        m = re.search(r'dev ([^ ]+)', route.strip())

        if m:
            return m.group(1)

    return None


def ensure_dirs():
    os.makedirs(ETC_DIR, exist_ok=True)
    os.makedirs(VM_CONFIG_DIR, exist_ok=True)

    os.makedirs(LIB_DIR, exist_ok=True)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    os.makedirs(VOLUME_DIR, exist_ok=True)


def detect_defaults():
    ensure_dirs()

    out_interface = conf.get('drakrun', 'out_interface')

    if not out_interface:
        default_if = find_default_interface()

        if default_if:
            logging.info(f"Detected default network interface: {default_if}")
            conf['drakrun']['out_interface'] = default_if
        else:
            logging.warning("Unable to detect default network interface.")


def ensure_zfs(ctx, param, value):
    if value is not None and ctx.params['storage_backend'] != "zfs":
        raise click.BadParameter("This parameter is valid only with ZFS backend")
    return value


def ensure_lvm(ctx, param, value):
    if value is not None and ctx.params['storage_backend'] != "lvm":
        raise click.BadParameter("This parameter is valid only with LVM backend")
    return value


def check_root():
    if os.getuid() != 0:
        logging.error("Please run the command as root")
        return False
    else:
        return True


def stop_all_drakruns():
    logging.info("Ensuring that drakrun@* services are stopped...")
    try:
        subprocess.check_output('systemctl stop \'drakrun@*\'', shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        raise Exception("Drakrun services not stopped")


def start_enabled_drakruns():
    logging.info("Starting previously stopped drakruns")
    enabled_services = set(list(get_enabled_drakruns()))
    wait_processes(
        "start services",
        [
            subprocess.Popen(
                ["systemctl", "start", service],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for service in enabled_services
        ],
    )


def cleanup_postinstall_files():
    for profile in os.listdir(PROFILE_DIR):
        safe_delete(os.path.join(PROFILE_DIR, profile))


@click.command(help='Cleanup the changes made by draksetup')
def cleanup():
    if not check_root():
        return

    install_info = InstallInfo.try_load()

    if install_info is None:
        logging.error("The cleanup has been performed")
        return

    stop_all_drakruns()

    backend = get_storage_backend(install_info)
    vm_ids = get_all_vm_conf()

    net_enable = int(conf['drakrun'].get('net_enable', '0'))
    out_interface = conf['drakrun'].get('out_interface', '')
    dns_server = conf['drakrun'].get('dns_server', '')

    for vm_id in vm_ids:
        vm = VirtualMachine(backend, vm_id)
        vm.destroy()

        delete_vm_network(vm_id=vm_id, net_enable=net_enable, out_interface=out_interface, dns_server=dns_server)
        if net_enable:
            stop_dnsmasq(vm_id=vm_id)

        backend.delete_vm_volume(vm_id)

        delete_vm_conf(vm_id)

    safe_delete(os.path.join(VOLUME_DIR, 'snapshot.sav'))
    cleanup_postinstall_files()

    InstallInfo.delete()


def sanity_check():
    logging.info("Checking xen-detect...")
    proc = subprocess.run('xen-detect -N', shell=True)

    if proc.returncode != 1:
        logging.error('It looks like the system is not running on Xen. Please reboot your machine into Xen hypervisor.')
        return False

    logging.info("Testing if xl tool is sane...")

    try:
        subprocess.run('xl info', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError:
        logging.exception("Failed to test xl info command. There might be some dependency problem (please execute 'xl info' manually to find out).")
        return False

    try:
        subprocess.run('xl list', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
    except subprocess.SubprocessError:
        logging.exception("Failed to test xl list command. There might be a problem with xen services (check 'systemctl status xenstored', 'systemctl status xenconsoled').")
        return False

    if not perform_xtf():
        logging.error("Your Xen installation doesn\'t pass the necessary tests.")
        return False

    return True


def perform_xtf():
    logging.info('Testing your Xen installation...')
    module_dir = os.path.dirname(os.path.realpath(__file__))
    cfg_path = os.path.join(module_dir, "tools", "test-hvm64-example.cfg")
    firmware_path = os.path.join(module_dir, "tools", "test-hvm64-example")

    with open(cfg_path, 'r') as f:
        test_cfg = f.read().replace('{{ FIRMWARE_PATH }}', firmware_path).encode('utf-8')

    with tempfile.NamedTemporaryFile() as tmpf:
        tmpf.write(test_cfg)
        tmpf.flush()

        logging.info('Checking if the test domain already exists...')
        subprocess.run('xl destroy test-hvm64-example', shell=True)

        logging.info('Creating new test domain...')
        subprocess.run(f'xl create -p {tmpf.name}', shell=True, stderr=subprocess.STDOUT, timeout=30, check=True)

        module_dir = os.path.dirname(os.path.realpath(__file__))
        test_altp2m_tool = os.path.join(module_dir, "tools", "test-altp2m")

        logging.info('Testing altp2m feature...')
        try:
            subprocess.run([test_altp2m_tool, 'test-hvm64-example'], stderr=subprocess.STDOUT, check=True)
        except subprocess.CalledProcessError as e:
            output = e.output.decode('utf-8', 'replace')
            logging.error(f'Failed to enable altp2m on domain. Your hardware might not support Extended Page Tables. Logs:\n{output}')
            subprocess.run('xl destroy test-hvm64-example', shell=True)
            return False

        logging.info('Performing simple XTF test...')
        p = subprocess.Popen(['xl', 'console', 'test-hvm64-example'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run('xl unpause test-hvm64-example', shell=True, stderr=subprocess.STDOUT, timeout=30, check=True)
        stdout_b, _ = p.communicate(timeout=10)

        stdout_text = stdout_b.decode('utf-8')
        stdout = [line.strip() for line in stdout_text.split('\n')]

        for line in stdout:
            if line == 'Test result: SUCCESS':
                logging.info('All tests passed. Your Xen installation seems to work properly.')
                return True

    logging.error(f'Preflight check with Xen Test Framework doesn\'t pass. Your hardware might not support VT-x. Logs: \n{stdout_text}')
    return False


@click.command(help='Perform self-test to check Xen installation')
def test():
    if not sanity_check():
        sys.exit(1)


@click.command(help='Install guest Virtual Machine',
               no_args_is_help=True)
@click.argument('iso_path', type=click.Path(exists=True))
@click.option('--vcpus', 'vcpus',
              default=2,
              type=int,
              show_default=True,
              help='Number of vCPUs per single VM')
@click.option('--memory', 'memory',
              default=3072,
              type=int,
              show_default=True,
              help='Memory per single VM (in MB)')
@click.option('--storage-backend', 'storage_backend',
              type=click.Choice(REGISTERED_BACKEND_NAMES, case_sensitive=False),
              default='qcow2',
              show_default=True,
              help='Storage backend', is_eager=True)
@click.option('--disk-size', 'disk_size',
              default='100G',
              show_default=True,
              help='Disk size')
@click.option('--zfs-tank-name', 'zfs_tank_name',
              callback=ensure_zfs,
              help='Tank name (only for ZFS storage backend)')
@click.option('--lvm-volume-group', 'lvm_volume_group',
              callback=ensure_lvm,
              help='Volume Group (only for lvm storage backend)'
              )
@click.option('--unattended-xml', 'unattended_xml',
              type=click.Path(exists=True),
              help='Path to autounattend.xml for automated Windows install')
def install(vcpus, memory, storage_backend, disk_size, iso_path, zfs_tank_name, lvm_volume_group, unattended_xml):
    if not check_root():
        return

    if storage_backend == "lvm" and lvm_volume_group is None:
        raise Exception("lvm storage backend requires --lvm-volume-group")
    if storage_backend == "zfs" and zfs_tank_name is None:
        raise Exception("zfs storage backend requires --zfs-tank-name")

    if not sanity_check():
        logging.error("Sanity check failed.")
        return

    stop_all_drakruns()

    logging.info("Performing installation...")

    if vcpus < 1:
        logging.error("Your VM must have at least 1 vCPU.")
        return

    if memory < 512:
        logging.error("Your VM must have at least 512 MB RAM.")
        return

    if memory < 1536:
        logging.warning("Using less than 1.5 GB RAM per VM is not recommended for any supported system.")

    if unattended_xml:
        logging.info("Baking unattended.iso for automated installation")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_xml_path = os.path.join(tmpdir, 'autounattend.xml')

            with open(tmp_xml_path, 'wb') as fw:
                with open(unattended_xml, 'rb') as fr:
                    fw.write(fr.read())

            try:
                subprocess.check_output(['genisoimage', '-o', os.path.join(VOLUME_DIR, "unattended.iso"), '-J', '-r', tmp_xml_path], stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError:
                logging.exception("Failed to generate unattended.iso.")

    sha256_hash = hashlib.sha256()

    logging.info("Calculating hash of iso")
    iso_file_size = os.stat(iso_path).st_size
    block_size = 128 * 1024
    with tqdm(total=iso_file_size, unit_scale=True) as pbar:
        with open(iso_path, "rb") as f:
            for byte_block in iter(lambda: f.read(block_size), b""):
                pbar.update(block_size)
                sha256_hash.update(byte_block)

            iso_sha256 = sha256_hash.hexdigest()

    install_info = InstallInfo(
        vcpus=vcpus,
        memory=memory,
        storage_backend=storage_backend,
        disk_size=disk_size,
        iso_path=os.path.abspath(iso_path),
        zfs_tank_name=zfs_tank_name,
        lvm_volume_group=lvm_volume_group,
        enable_unattended=unattended_xml is not None,
        iso_sha256=iso_sha256
    )
    install_info.save()

    try:
        subprocess.check_output('xl uptime vm-0', shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        pass
    else:
        logging.info('Detected that vm-0 is already running, stopping it.')
        subprocess.run('xl destroy vm-0', shell=True, check=True)

    generate_vm_conf(install_info, 0)

    backend = get_storage_backend(install_info)
    backend.initialize_vm0_volume(disk_size)

    try:
        subprocess.check_output("brctl show", shell=True)
    except subprocess.CalledProcessError:
        logging.exception("Failed to execute brctl show. Make sure you have bridge-utils installed.")
        return

    net_enable = conf['drakrun'].getboolean('net_enable', fallback=False)
    out_interface = conf['drakrun'].get('out_interface', '')
    dns_server = conf['drakrun'].get('dns_server', '')

    setup_vm_network(vm_id=0, net_enable=net_enable, out_interface=out_interface, dns_server=dns_server)

    if net_enable:
        start_dnsmasq(vm_id=0, dns_server=dns_server, background=True)

    cfg_path = os.path.join(VM_CONFIG_DIR, "vm-0.cfg")

    try:
        subprocess.run('xl create {}'.format(shlex.quote(cfg_path)), shell=True, check=True)
    except subprocess.CalledProcessError:
        logging.exception("Failed to launch VM vm-0")
        return

    logging.info("-" * 80)
    logging.info("Initial VM setup is complete and the vm-0 was launched.")
    logging.info("Please now VNC to the port 5900 on this machine to perform Windows installation.")
    logging.info("After you have installed Windows and booted it to the desktop, please execute:")
    logging.info("# draksetup postinstall")

    with open(cfg_path, "r") as f:
        data = f.read()
        m = re.search(r'vncpasswd[ ]*=(.*)', data)
        if m:
            passwd = m.group(1).strip()
            if passwd[0] == '"' and passwd[-1] == '"':
                passwd = passwd[1:-1]

            logging.info("Your configured VNC password is:")
            logging.info(passwd)

    logging.info("Please note that on some machines, system installer may boot for up to 10 minutes")
    logging.info("and may look unresponsive during the process. Please be patient.")
    logging.info("-" * 80)


def send_usage_report(report):
    try:
        res = requests.post('https://drakvuf.icedev.pl/usage/draksetup', json=report, timeout=5)
        res.raise_for_status()
    except RequestException:
        logging.exception("Failed to send usage report. This is not a serious problem.")


def create_rekall_profile(injector: Injector, file: DLL):
    tmp = None
    try:
        logging.info(f"Fetching rekall profile for {file.path}")

        local_dll_path = os.path.join(PROFILE_DIR, file.dest)
        guest_dll_path = str(PureWindowsPath("C:/", file.path))

        cmd = injector.read_file(guest_dll_path, local_dll_path)
        out = json.loads(cmd.stdout.decode())
        if out["Status"] == "Error" and out["Error"] in ["ERROR_FILE_NOT_FOUND", "ERROR_PATH_NOT_FOUND"]:
            raise FileNotFoundError
        if out["Status"] != "Success":
            logging.debug("stderr: " + cmd.stderr.decode())
            logging.debug(out)
            # Take care if the error message is changed
            raise Exception("Some error occurred in injector")

        guid = pdb_guid(local_dll_path)
        tmp = fetch_pdb(guid["filename"], guid["GUID"], PROFILE_DIR)

        logging.debug("Parsing PDB into JSON profile...")
        profile = make_pdb_profile(
            tmp,
            dll_origin_path=guest_dll_path,
            dll_path=local_dll_path
        )
        with open(os.path.join(PROFILE_DIR, f"{file.dest}.json"), 'w') as f:
            f.write(profile)
    except json.JSONDecodeError:
        logging.debug(f"stdout: {cmd.stdout}")
        logging.debug(f"stderr: {cmd.stderr}")
        logging.debug(traceback.format_exc())
        raise Exception(f"Failed to parse json response on {file.path}")
    except FileNotFoundError:
        logging.warning(f"Failed to copy file {file.path}, skipping...")
    except RuntimeError:
        logging.warning(f"Failed to fetch profile for {file.path}, skipping...")
    except Exception as e:
        # Take care if the error message is changed
        if str(e) == "Some error occurred in injector":
            raise
        else:
            logging.warning(f"Unexpected exception while creating rekall profile for {file.path}, skipping...")
            # Can help in debugging
            logging.debug("stderr: " + cmd.stderr.decode())
            logging.debug(out)
            logging.debug(traceback.format_exc())
    finally:
        safe_delete(local_dll_path)
        # was crashing here if the first file reached some exception
        if tmp is not None:
            safe_delete(os.path.join(PROFILE_DIR, tmp))


def extract_explorer_pid(
    domain: str,
    kernel_profile: str,
    offsets: VmiOffsets,
    timeout: int = 30
) -> Optional[int]:
    """ Call get-explorer-pid helper and get its PID """
    module_dir = os.path.dirname(os.path.realpath(__file__))
    pid_tool = os.path.join(module_dir, "tools", "get-explorer-pid")
    try:
        explorer_pid_s = subprocess.check_output([
            pid_tool,
            domain,
            kernel_profile,
            hex(offsets.kpgd)
        ], timeout=timeout).decode('utf-8', 'ignore')

        m = re.search(r'explorer\.exe:([0-9]+)', explorer_pid_s)
        if m is not None:
            return int(m.group(1))

    except subprocess.CalledProcessError:
        logging.exception("get-explorer-pid exited with an error")
    except subprocess.TimeoutExpired:
        logging.exception("get-explorer-pid timed out")

    raise RuntimeError("Extracting explorer PID failed")


def extract_vmi_offsets(
    domain: str,
    kernel_profile: str,
    timeout: int = 30
) -> Optional[VmiOffsets]:
    """ Call vmi-win-offsets helper and obtain VmiOffsets values """
    try:
        output = subprocess.check_output([
            'vmi-win-offsets',
            '--name', domain,
            '--json-kernel', kernel_profile
        ], timeout=timeout).decode('utf-8', 'ignore')

        return VmiOffsets.from_tool_output(output)
    except TypeError:
        logging.exception("Invalid output of vmi-win-offsets")
    except subprocess.CalledProcessError:
        logging.exception("vmi-win-offsets exited with an error")
    except subprocess.TimeoutExpired:
        logging.exception("vmi-win-offsets timed out")

    raise RuntimeError("Extracting VMI offsets failed")


def eject_cd(domain, drive):
    subprocess.run(["xl", "cd-eject", domain, drive], check=True)


def insert_cd(domain, drive, iso):
    subprocess.run(["xl", "cd-insert", domain, drive, iso], check=True)


@click.command()
@click.option('--report/--no-report', 'report',
              default=True,
              show_default=True,
              help="Send anonymous usage report")
@click.option('--usermode/--no-usermode', 'generate_usermode',
              default=True,
              show_default=True,
              help="Generate user mode profiles")
def postinstall(report, generate_usermode):
    if not check_root():
        return

    if os.path.exists(os.path.join(ETC_DIR, "no_usage_reports")):
        report = False

    install_info = InstallInfo.load()
    storage_backend = get_storage_backend(install_info)

    vm = VirtualMachine(storage_backend, 0)

    if vm.is_running is False:
        logging.exception("vm-0 is not running")
        return

    logging.info("Cleaning up leftovers(if any)")
    cleanup_postinstall_files()

    logging.info("Ejecting installation CDs")
    eject_cd("vm-0", FIRST_CDROM_DRIVE)
    if install_info.enable_unattended:
        # If unattended install is enabled, we have an additional CD-ROM drive
        eject_cd("vm-0", SECOND_CDROM_DRIVE)

    output = subprocess.check_output(['vmi-win-guid', 'name', 'vm-0'], timeout=30).decode('utf-8')

    try:
        version = re.search(r'Version: (.*)', output).group(1)
        pdb = re.search(r'PDB GUID: ([0-9a-f]+)', output).group(1)
        fn = re.search(r'Kernel filename: ([a-z]+\.[a-z]+)', output).group(1)
    except AttributeError:
        logging.error("Failed to obtain kernel PDB GUID/Kernel filename.")
        return

    logging.info("Determined PDB GUID: {}".format(pdb))
    logging.info("Determined kernel filename: {}".format(fn))

    logging.info("Fetching PDB file...")
    dest = fetch_pdb(fn, pdb, destdir=PROFILE_DIR)

    logging.info("Generating profile out of PDB file...")
    profile = make_pdb_profile(dest)

    logging.info("Saving profile...")
    kernel_profile = os.path.join(PROFILE_DIR, 'kernel.json')
    with open(kernel_profile, 'w') as f:
        f.write(profile)

    safe_delete(dest)

    vmi_offsets = extract_vmi_offsets('vm-0', kernel_profile)
    explorer_pid = extract_explorer_pid('vm-0', kernel_profile, vmi_offsets)
    runtime_info = RuntimeInfo(vmi_offsets=vmi_offsets, inject_pid=explorer_pid)

    logging.info("Saving runtime profile...")
    with open(os.path.join(PROFILE_DIR, 'runtime.json'), 'w') as f:
        f.write(runtime_info.to_json(indent=4))

    logging.info("Saving VM snapshot...")

    # snapshot domain but don't destroy it, leave it in paused state
    subprocess.check_output('xl save -p vm-0 ' + os.path.join(VOLUME_DIR, "snapshot.sav"), shell=True)
    logging.info("Snapshot was saved succesfully.")

    logging.info("Snapshotting persistent memory...")
    storage_backend.snapshot_vm0_volume()

    logging.info("Unpausing VM")
    subprocess.check_output('xl unpause vm-0', shell=True)

    injector = Injector('vm-0', runtime_info, kernel_profile)
    if generate_usermode:
        try:
            for file in dll_file_list:
                create_rekall_profile(injector, file)
        except RuntimeError as e:
            logging.warning("Generating usermode profiles failed")
            logging.exception(e)

    subprocess.check_output('xl destroy vm-0', shell=True)

    if report:
        send_usage_report({
            "kernel": {
                "guid": pdb,
                "filename": fn,
                "version": version
            },
            "install_iso": {
                "sha256": install_info.iso_sha256
            }
        })

    logging.info("All right, drakrun setup is done.")
    logging.info("First instance of drakrun will be enabled automatically...")
    subprocess.check_output('systemctl enable drakrun@1', shell=True)
    subprocess.check_output('systemctl start drakrun@1', shell=True)

    logging.info("If you want to have more parallel instances, execute:")
    logging.info("  # draksetup scale <number of instances>")


def profile_exists(profile: DLL) -> bool:
    return (Path(PROFILE_DIR) / f"{profile.dest}.json").is_file()


def create_missing_profiles(injector: Injector):
    # Ensure that all declared usermode profiles exist
    # This is important when upgrade defines new entries in dll_file_list
    for profile in dll_file_list:
        if not profile_exists(profile):
            create_rekall_profile(injector, profile)


@click.command(help='Perform tasks after drakrun upgrade')
def postupgrade():
    if not check_root():
        return

    with open(os.path.join(ETC_DIR, 'scripts/cfg.template'), 'r') as f:
        template = f.read()

    passwd_characters = string.ascii_letters + string.digits
    passwd = ''.join(secrets.choice(passwd_characters) for _ in range(20))
    template = template.replace('{{ VNC_PASS }}', passwd)

    with open(os.path.join(ETC_DIR, 'scripts', 'cfg.template'), 'w') as f:
        f.write(template)

    detect_defaults()

    install_info = InstallInfo.try_load()
    if not install_info:
        logging.info("Postupgrade done. DRAKVUF Sandbox not installed.")
        return

    # Prepare injector
    with open(os.path.join(PROFILE_DIR, "runtime.json"), 'r') as runtime_f:
        runtime_info = RuntimeInfo.load(runtime_f)
    kernel_profile = os.path.join(PROFILE_DIR, "kernel.json")
    injector = Injector('vm-1', runtime_info, kernel_profile)

    stop_all_drakruns()

    # Use vm-1 for generating profiles
    out_interface = conf['drakrun'].get('out_interface', '')
    dns_server = conf['drakrun'].get('dns_server', '')
    setup_vm_network(vm_id=1, net_enable=False, out_interface=out_interface, dns_server=dns_server)
    backend = get_storage_backend(install_info)
    vm = VirtualMachine(backend, 1)
    vm.restore()

    create_missing_profiles(injector)

    vm.destroy()
    delete_vm_network(vm_id=1, net_enable=False, out_interface=out_interface, dns_server=dns_server)
    start_enabled_drakruns()


def get_enabled_drakruns():
    for fn in os.listdir("/etc/systemd/system/default.target.wants"):
        if re.fullmatch('drakrun@[0-9]+\\.service', fn):
            yield fn


def wait_processes(descr, popens):
    total = len(popens)

    if total == 0:
        return True

    exit_codes = []

    with tqdm(total=total, unit_scale=True) as pbar:
        pbar.set_description(descr)
        while True:
            time.sleep(0.25)
            for popen in popens:
                exit_code = popen.poll()
                if exit_code is not None:
                    exit_codes.append(exit_code)
                    popens.remove(popen)
                    pbar.update(1)

            if len(popens) == 0:
                return all([exit_code == 0 for exit_code in exit_codes])


@click.command(help='Scale drakrun services',
               no_args_is_help=True)
@click.argument('scale_count',
                type=int)
def scale(scale_count):
    """Enable or disable additional parallel instances of drakrun service.."""
    if scale_count < 1:
        raise RuntimeError('Invalid value of scale parameter. Must be at least 1.')

    cur_services = set(list(get_enabled_drakruns()))
    new_services = set([f'drakrun@{i}.service' for i in range(1, scale_count + 1)])

    disable_services = cur_services - new_services
    enable_services = new_services

    wait_processes('disable services', [subprocess.Popen(["systemctl", "disable", service], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for service in disable_services])
    wait_processes('enable services', [subprocess.Popen(["systemctl", "enable", service], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for service in enable_services])
    wait_processes('start services', [subprocess.Popen(["systemctl", "start", service], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for service in enable_services])
    wait_processes('stop services', [subprocess.Popen(["systemctl", "stop", service], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for service in disable_services])


@click.command(help='Mount ISO into guest',
               no_args_is_help=True)
@click.argument('iso_path',
                type=click.Path(exists=True))
@click.option('--domain', 'domain_name',
              type=str,
              default='vm-0',
              show_default=True,
              help='Domain name (i.e. Virtual Machine name)')
def mount(iso_path, domain_name):
    '''Inject ISO file into specified guest vm.
    Domain can be retrieved by running "xl list" command on the host.
    '''
    iso_path_full = os.path.abspath(iso_path)
    insert_cd(domain_name, FIRST_CDROM_DRIVE, iso_path_full)


def get_minio_client(config):
    minio_cfg = config['minio']
    return Minio(endpoint=minio_cfg['address'],
                 access_key=minio_cfg['access_key'],
                 secret_key=minio_cfg['secret_key'],
                 secure=minio_cfg.getboolean('secure', fallback=True))


@click.group(help="Manage VM snapshots")
def snapshot():
    pass


@snapshot.command(name='export', help='Upload local snapshot to MinIO.', no_args_is_help=True)
@click.option('--name', required=True, help='Name of the snapshot')
@click.option('--bucket', default='drakrun-snapshots', help='MinIO bucket to store the snapshot')
@click.option('--full', default=False, is_flag=True, help='Upload memory snapshot and profiles')
@click.option('--force', default=False, is_flag=True, help='Overwrite remote snapshot')
def snapshot_export(name, bucket, full, force):
    install_info = InstallInfo.try_load()
    if install_info is None:
        logging.error("Missing installation info. Did you forget to set up the sandbox?")
        return

    mc = get_minio_client(conf)

    if not mc.bucket_exists(bucket):
        logging.error("Bucket %s doesn't exist", bucket)
        return

    if len(list(mc.list_objects(bucket, f"{name}/"))) > 0 and not force:
        logging.error("There are objects in bucket %s at path %s. Aborting...", bucket, f"{name}/")
        return

    logging.info("Exporting snapshot as %s into %s", name, bucket)

    if full:
        logging.warning("Full snapshots may not work if hardware used for "
                        "importing and exporting differs. You have been warned!")
        do_export_full(mc, bucket, name)
    else:
        do_export_minimal(mc, bucket, name)

    logging.info("Done. To use exported snapshot on other machine, execute:")
    logging.info("# draksetup snapshot import --name %s --bucket %s", name, bucket)


@snapshot.command(name='import', help='Download and configure remote snapshot', no_args_is_help=True)
@click.option('--name', required=True, help='Name of the exported snapshot')
@click.option('--bucket', default='drakrun-snapshots', help='MinIO bucket to store the snapshot')
@click.option('--full', default=False, is_flag=True, help='Download VM memory and profiles')
@click.option('--zpool', help='Override zpool name stored in snapshot (only for ZFS snapshots)')
def snapshot_import(name, bucket, full, zpool):
    local_install = InstallInfo.try_load()
    if local_install is not None:
        click.confirm("Detected local snapshot. It will be REMOVED. Continue?", abort=True)

    mc = get_minio_client(conf)

    if not mc.bucket_exists(bucket):
        logging.error("Bucket %s doesn't exist", bucket)
        return

    ensure_dirs()

    try:
        if full:
            logging.warning("Importing full snapshot. This may not work if hardware is different")
            do_import_full(mc, name, bucket, zpool)
        else:
            do_import_minimal(mc, name, bucket, zpool)

            # This could probably use some refactoring
            # We're duplicating quite a lot of code from install function
            install_info = InstallInfo.load()
            generate_vm_conf(install_info, 0)
            backend = get_storage_backend(install_info)
            backend.rollback_vm_storage(0)

            net_enable = int(conf['drakrun'].get('net_enable', '0'))
            out_interface = conf['drakrun'].get('out_interface', '')
            dns_server = conf['drakrun'].get('dns_server', '')
            setup_vm_network(
                vm_id=0,
                net_enable=net_enable,
                out_interface=out_interface,
                dns_server=dns_server
            )

            if net_enable:
                start_dnsmasq(vm_id=0, dns_server=dns_server, background=True)

            cfg_path = os.path.join(VM_CONFIG_DIR, "vm-0.cfg")

            try:
                subprocess.run(['xl' 'create', cfg_path], check=True)
            except subprocess.CalledProcessError:
                logging.exception("Failed to launch VM vm-0")
                return

            logging.info("Minimal snapshots require postinstall to work correctly")
            logging.info("Please VNC to the port 5900 to ensure the OS booted correctly")
            logging.info("After that, execute this command to finish the setup")
            logging.info("# draksetup postinstall")
    except NoSuchKey:
        logging.error("Import failed. Missing files in bucket.")


def do_export_minimal(mc, bucket, name):
    """ Perform minimal snapshot export, symmetric to do_import_minimal """
    logging.info("Uploading installation info")
    install_info = InstallInfo.load()
    install_data = json.dumps(install_info.to_dict()).encode()
    mc.put_object(bucket, f"{name}/install.json", io.BytesIO(install_data), len(install_data))

    logging.info("Uploading VM template")
    mc.fput_object(bucket, f"{name}/cfg.template", os.path.join(ETC_DIR, "scripts", "cfg.template"))

    with tempfile.NamedTemporaryFile() as disk_image:
        logging.info("Exporting VM hard drive")
        storage = get_storage_backend(install_info)
        storage.export_vm0(disk_image.name)

        logging.info("Uploading disk.img")
        mc.fput_object(bucket, f"{name}/disk.img", disk_image.name)


def do_import_minimal(mc, name, bucket, zpool):
    """ Perform minimal snapshot import, symmetric to do_export_minimal """
    logging.info("Downloading installation info")
    mc.fget_object(bucket, f"{name}/install.json",
                   os.path.join(ETC_DIR, InstallInfo._INSTALL_FILENAME))

    logging.info("Downloading VM config")
    mc.fget_object(bucket, f"{name}/cfg.template", os.path.join(ETC_DIR, "scripts", "cfg.template"))

    # Now we have imported InstallInfo object
    install_info = InstallInfo.load()

    # Patch ZFS pool name
    if zpool is not None:
        install_info.zfs_tank_name = zpool
        # Save patched ZFS dataset name (storage backend has to know it)
        install_info.save()

    storage = get_storage_backend(install_info)

    with tempfile.NamedTemporaryFile() as disk_image:
        logging.info("Downloading VM disk image")
        mc.fget_object(bucket, f"{name}/disk.img", disk_image.name)

        logging.info("Importing VM disk")
        storage.import_vm0(disk_image.name)


def do_export_full(mc, bucket, name):
    """ Perform full snapshot export, symmetric to do_import_full """
    do_export_minimal(mc, bucket, name)

    with tempfile.NamedTemporaryFile() as compressed_snapshot:
        # Compress snapshot
        logging.info("Compressing snapshot.sav")
        subprocess.check_call(
            ["gzip", "-c", os.path.join(VOLUME_DIR, "snapshot.sav")],
            stdout=compressed_snapshot,
        )

        logging.info("Uploading snapshot.sav.gz")
        mc.fput_object(bucket, f"{name}/snapshot.sav.gz", compressed_snapshot.name)

    # Upload profiles
    for file in os.listdir(PROFILE_DIR):
        logging.info("Uploading profile %s", file)
        mc.fput_object(bucket, f"{name}/profiles/{file}", os.path.join(PROFILE_DIR, file))


def do_import_full(mc, name, bucket, zpool):
    """ Perform full snapshot import, symmetric to do_export_full """
    do_import_minimal(mc, name, bucket, zpool)

    with tempfile.NamedTemporaryFile() as compressed_snapshot:
        mc.fget_object(bucket, f"{name}/snapshot.sav.gz", compressed_snapshot.name)

        logging.info("Decompressing VM snapshot")
        with open(os.path.join(VOLUME_DIR, "snapshot.sav"), "wb") as snapshot:
            subprocess.run(
                ["zcat", compressed_snapshot.name],
                stdout=snapshot,
                check=True
            )

    profile_prefix = f"{name}/profiles/"
    for object in mc.list_objects(bucket, prefix=profile_prefix):
        # Strip profile prefix
        profile_name = object.object_name[len(profile_prefix):]
        mc.fget_object(bucket, object.object_name, os.path.join(PROFILE_DIR, profile_name))


@click.group()
def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format='[%(asctime)s][%(levelname)s] %(message)s',
        handlers=[logging.StreamHandler()]
    )


main.add_command(test)
main.add_command(install)
main.add_command(postinstall)
main.add_command(postupgrade)
main.add_command(mount)
main.add_command(scale)
main.add_command(snapshot)
main.add_command(cleanup)


if __name__ == "__main__":
    if os.geteuid() != 0:
        logging.warning('Not running as root, draksetup may work improperly!')
    main()
