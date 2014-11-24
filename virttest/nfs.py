"""
Basic nfs support for Linux host. It can support the remote
nfs mount and the local nfs set up and mount.
"""
import re
import os
import shutil
import logging
import commands
from autotest.client import os_dep
from autotest.client.shared import utils, error
from virttest import utils_misc
from virttest.utils_conn import SSHConnection

from virttest.staging import service


def nfs_exported():
    """
    Get the list for nfs file system already exported

    :return: a list of nfs that is already exported in system
    :rtype: a lit of nfs file system exported
    """
    exportfs = utils.system_output("exportfs -v")
    if not exportfs:
        return {}

    nfs_exported_dict = {}
    for fs_info in re.findall("[/\w+]+.*?\(.*?\)", exportfs, re.S):
        fs_info = fs_info.strip().split()
        if len(fs_info) == 2:
            nfs_src = fs_info[0]
            access_ip = re.findall(r"(.*)\(", fs_info[1])[0]
            if "world" in access_ip:
                access_ip = "*"
            nfs_tag = "%s_%s" % (nfs_src, access_ip)
            permission = re.findall(r"\((.*)\)", fs_info[1])[0]
            nfs_exported_dict[nfs_tag] = permission

    return nfs_exported_dict


class Exportfs(object):

    """
    Add or remove one entry to exported nfs file system.
    """

    def __init__(self, path, client="*", options="", ori_exported=None):
        if ori_exported is None:
            ori_exported = []
        self.path = path
        self.client = client
        self.options = options.split(",")
        self.ori_exported = ori_exported
        self.entry_tag = "%s_%s" % (self.path, self.client)
        self.already_exported = False
        self.ori_options = ""

    def is_exported(self):
        """
        Check if the directory is already exported.

        :return: If the entry is exported
        :rtype: Boolean
        """
        ori_exported = self.ori_exported or nfs_exported()
        if self.entry_tag in ori_exported.keys():
            return True
        return False

    def need_reexport(self):
        """
        Check if the entry is already exported but the options are not
        the same as we required.

        :return: Need re export the entry or not
        :rtype: Boolean
        """
        ori_exported = self.ori_exported or nfs_exported()
        if self.is_exported():
            exported_options = ori_exported[self.entry_tag]
            options = [_ for _ in self.options if _ not in exported_options]
            if options:
                self.ori_options = exported_options
                return True
        return False

    def unexport(self):
        """
        Unexport an entry.
        """
        if self.is_exported():
            unexport_cmd = "exportfs -u %s:%s" % (self.client, self.path)
            utils.system(unexport_cmd)
        else:
            logging.warn("Target %s %s is not exported yet."
                         "Can not unexport it." % (self.client, self.path))

    def reset_export(self):
        """
        Reset the exportfs to the original status before we export the
        specific entry.
        """
        self.unexport()
        if self.ori_options:
            tmp_options = self.options
            self.options = self.ori_options.split(",")
            self.export()
            self.options = tmp_options

    def export(self):
        """
        Export one directory if it is not in exported list.

        :return: Export nfs file system succeed or not
        """
        if self.is_exported():
            if self.need_reexport():
                self.unexport()
            else:
                self.already_exported = True
                logging.warn("Already exported target."
                             " Don't need export it again")
                return True
        export_cmd = "exportfs"
        if self.options:
            export_cmd += " -o %s" % ",".join(self.options)
        export_cmd += " %s:%s" % (self.client, self.path)
        try:
            utils.system(export_cmd)
        except error.CmdError, export_failed_err:
            logging.error("Can not export target: %s" % export_failed_err)
            return False
        return True


class Nfs(object):

    """
    Nfs class for handle nfs mount and umount. If a local nfs service is
    required, it will configure a local nfs server accroding the params.
    """

    def __init__(self, params):
        self.mount_dir = params.get("nfs_mount_dir")
        self.mount_options = params.get("nfs_mount_options")
        self.mount_src = params.get("nfs_mount_src")
        self.nfs_setup = False
        os_dep.command("mount")
        self.mk_mount_dir = False
        self.unexportfs_in_clean = False

        if params.get("setup_local_nfs") == "yes":
            self.nfs_setup = True
            os_dep.command("service")
            os_dep.command("exportfs")
            self.nfs_service = service.Factory.create_service("nfs")

            self.export_dir = (params.get("export_dir")
                               or self.mount_src.split(":")[-1])
            self.export_ip = params.get("export_ip", "*")
            self.export_options = params.get("export_options", "").strip()
            self.exportfs = Exportfs(self.export_dir, self.export_ip,
                                     self.export_options)
            self.mount_src = "127.0.0.1:%s" % self.export_dir

    def is_mounted(self):
        """
        Check the NFS is mounted or not.

        :return: If the src is mounted as expect
        :rtype: Boolean
        """
        return utils_misc.is_mounted(self.mount_src, self.mount_dir, "nfs")

    def mount(self):
        """
        Mount source into given mount point.
        """
        return utils_misc.mount(self.mount_src, self.mount_dir, "nfs",
                                perm=self.mount_options)

    def umount(self):
        """
        Umount the given mount point.
        """
        return utils_misc.umount(self.mount_src, self.mount_dir, "nfs")

    def setup(self):
        """
        Setup NFS in host.

        Mount NFS as configured. If a local nfs is requested, setup the NFS
        service and exportfs too.
        """
        if self.nfs_setup:
            if not self.nfs_service.status():
                logging.debug("Restart NFS service.")
                self.nfs_service.restart()

            if not os.path.isdir(self.export_dir):
                os.makedirs(self.export_dir)
            self.exportfs.export()
            self.unexportfs_in_clean = not self.exportfs.already_exported

        logging.debug("Mount %s to %s" % (self.mount_src, self.mount_dir))
        if os.path.exists(self.mount_dir) and not os.path.isdir(self.mount_dir):
            raise OSError(
                "Mount point %s is not a directory, check your setup." %
                self.mount_dir)

        if not os.path.isdir(self.mount_dir):
            os.makedirs(self.mount_dir)
            self.mk_mount_dir = True
        self.mount()

    def cleanup(self):
        """
        Clean up the host env.

        Umount NFS from the mount point. If there has some change for exported
        file system in host when setup, also clean up that.
        """
        self.umount()
        if self.nfs_setup and self.unexportfs_in_clean:
            self.exportfs.reset_export()
        if self.mk_mount_dir and os.path.isdir(self.mount_dir):
            shutil.rmtree(self.mount_dir)


class NFSClient(object):

    """
    NFSClient class for handle nfs remotely mount and umount.
    """

    def __init__(self, params):
        # Setup SSH connection
        self.ssh_obj = SSHConnection(params)
        self.ssh_obj.conn_setup()

        self.mkdir_mount_remote = False
        self.mount_dir = params.get("nfs_mount_dir")
        self.mount_options = params.get("nfs_mount_options")
        self.mount_src = params.get("nfs_mount_src")
        self.nfs_client_ip = params.get("nfs_client_ip")
        self.nfs_server_ip = params.get("nfs_server_ip")
        self.ssh_user = params.get("ssh_username", "root")
        self.remote_nfs_mount = params.get("remote_nfs_mount", "yes")

    def is_mounted(self):
        """
        Check the NFS is mounted or not.

        :return: If the src is mounted as expect
        :rtype: Boolean
        """
        ssh_cmd = "ssh %s@%s " % (self.ssh_user, self.nfs_client_ip)
        find_mountpoint_cmd = "mount | grep -E '.*%s.*%s.*'" % (self.mount_src,
                                                                self.mount_dir)
        cmd = ssh_cmd + "'%s'" % find_mountpoint_cmd
        logging.debug("The command: %s", cmd)
        status, output = commands.getstatusoutput(cmd)
        if status:
            logging.debug("The command result: <%s:%s>", status, output)
            return False

        return True

    def setup(self):
        """
        Setup NFS client.
        """
        # Mount sharing directory to local host
        # it has been covered by class Nfs

        # Mount sharing directory to remote host
        if self.remote_nfs_mount == "yes":
            self.setup_remote()

    def cleanup(self):
        """
        Cleanup NFS client.
        """
        ssh_cmd = "ssh %s@%s " % (self.ssh_user, self.nfs_client_ip)
        logging.debug("Umount %s from %s" % (self.mount_dir, self.nfs_server_ip))
        umount_cmd = ssh_cmd + "'umount -l %s'" % self.mount_dir
        try:
            utils.system(umount_cmd, verbose=True)
        except error.CmdError:
            raise error.TestFail("Failed to run: %s", umount_cmd)

        if self.mkdir_mount_remote:
            rmdir_cmd = ssh_cmd + "'rm -rf %s'" % self.mount_dir
            try:
                utils.system(rmdir_cmd, verbose=True)
            except error.CmdError:
                raise error.TestFail("Failed to run: %s", rmdir_cmd)

        if self.is_mounted():
            raise error.TestFail("Failed to umount %s", self.mount_dir)

        # Recover SSH connection
        self.ssh_obj.auto_recover = True
        del self.ssh_obj

    def setup_remote(self):
        """
        Mount sharing directory to remote host.
        """
        ssh_cmd = "ssh %s@%s " % (self.ssh_user, self.nfs_client_ip)
        check_mount_dir_cmd = ssh_cmd + "'ls -d %s'" % self.mount_dir
        logging.debug("To check if the %s exists", self.mount_dir)
        output = commands.getoutput(check_mount_dir_cmd)
        if re.findall("No such file or directory", output, re.M):
            mkdir_cmd = ssh_cmd + "'mkdir -p %s'" % self.mount_dir
            logging.debug("Prepare to create %s", self.mount_dir)
            s, o = commands.getstatusoutput(mkdir_cmd)
            if s != 0:
                raise error.TestFail("Failed to run %s: %s", mkdir_cmd, o)
            self.mkdir_mount_remote = True

        self.mount_src = "%s:%s" % (self.nfs_server_ip, self.mount_src)
        logging.debug("Mount %s to %s" % (self.mount_src, self.mount_dir))
        mount_cmd = ssh_cmd + "'mount -t nfs %s %s -o %s'" % (self.mount_src,
                                                              self.mount_dir,
                                                              self.mount_options)
        try:
            utils.system(mount_cmd, verbose=True)
        except error.CmdError:
            raise error.TestFail("Failed to run: %s", mount_cmd)

        # Check if the sharing directory is mounted
        if not self.is_mounted():
            raise error.TestFail("Failed to mount from %s to %s",
                                 self.mount_src, self.mount_dir)
