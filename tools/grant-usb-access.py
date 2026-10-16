"""Linux environment helper. Run once as root after USB re-enumeration.

Only changes permissions for the connected GLXSS bootloader/G25 device node.
For persistent access, install the supplied udev rules instead.
"""

import grp
import os
from pathlib import Path

import usb.core

group = grp.getgrnam("plugdev").gr_gid
for vid, pid in ((0x03E7, 0x2150), (0x2E09, 0x0030)):
    for device in usb.core.find(find_all=True, idVendor=vid, idProduct=pid):
        node = Path(f"/dev/bus/usb/{device.bus:03}/{device.address:03}")
        os.chown(node, -1, group)
        os.chmod(node, 0o660)
        print(f"{vid:04x}:{pid:04x} {node}: group=plugdev mode=0660")
