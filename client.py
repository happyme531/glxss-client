import usb.core
import usb.util

import sys, time
import coloredlogs, logging

logger = logging.getLogger(__name__)
coloredlogs.install(level='DEBUG')

fw_path = "G25_MainFW_1.9.2.decrypted.img"
# fw_path = "G26_PRO_RELEASE_10000051.img"

device_vid = 0x2e09
device_pid = 0x0030
# device_pid = 0x0041

def load_fw():
    bootloader_vid = 0x03e7  
    bootloader_pid = 0x2150
    dev: usb.core.Device = usb.core.find(idVendor=bootloader_vid, idProduct=bootloader_pid)
    if dev is None:
        logger.error("Device not found, please try again")
    else:
        logger.info(f"Found Movidius MA2X5X bootloader device at bus {dev.bus} device {dev.address}")
        dev.set_configuration()
        with open(fw_path, "rb") as f:
            data = f.read()
            fw_size = len(data)
            logger.info(f"Firmware loaded from path: {fw_path}")
            start_time = time.time()
            dev.write(0x01, data)
            logger.info(f"loaded firmware of {fw_size / 1024:.1f} kiB in {(time.time() - start_time) * 1000:.1f} ms = {(fw_size / 1024) / (time.time() - start_time):.1f} kiB/s")
            logger.info("Firmware loaded successfully")

device: usb.core.Device = None

def wait_for_device(timeout_ms = 5000):
    logger.info("Waiting for device")
    start_time = time.time()
    while True:
        # check if the vid/pid is correct
        device = usb.core.find(idVendor=device_vid, idProduct=device_pid)
        if device is not None:
            break
        if time.time() - start_time > timeout_ms / 1000:
            logger.error("Device not found, please try again")
            sys.exit(1)
        time.sleep(0.1)
    logger.info("Device found")


def open_device():
    global device
    device = usb.core.find(idVendor=device_vid, idProduct=device_pid)
    if device is None:
        logger.error("Device not found, please try again")
    else:
        logger.info(f"Found device at bus {device.bus} device {device.address}")
        # device.set_configuration()

def lcd_get_info():
    # do a control transfer to get the LCD info
    # request type: 0xa1
    # request: 0x04
    # value: 0x00
    # index: 0x03
    # length: 0x08

    data = device.ctrl_transfer(0xa1, 0x04, 0x00, 0x03, 0x08)
    # int16_t width;
    # int16_t height;
    # int8_t orientation;
    # int8_t rotation;
    # int16_t brightness;

    width = data[0] | (data[1] << 8)
    height = data[2] | (data[3] << 8)
    orientation = data[4]
    rotation = data[5]
    brightness = data[6] | (data[7] << 8)

    # "LCD info: width: %d, height: %d, orientation: %d, rotation: %d, brightness: %d"
    logger.info(f"LCD info: width: {width}, height: {height}, orientation: {orientation}, rotation: {rotation}, brightness: {brightness}")
    return {
        "width": width,
        "height": height,
        "orientation": orientation,
        "rotation": rotation,
        "brightness": brightness
    }

def lcd_xfer_image(width, height, data):
    # packet:
    # int width
    # int height
    # uint8_t format = 1
    # uint8_t fmt_reserved[3] = {0, 0, 0}
    # uint8_t reserved[8] = {0, 0, 0, 0, 0, 0, 0, 0}
    # uint8_t data[]

    buf = bytearray()
    buf += width.to_bytes(4, byteorder="little")
    buf += height.to_bytes(4, byteorder="little")
    buf += b"\x01\x00\x00\x00\x00\x00\x00\x00"
    buf += data

    # bulk transfer to ep1
    device.write(0x01, buf)

def enable_camera():
    # do a control transfer to enable the camera
    # request type: 0x21
    # request: 0x01
    # value: 0xd0d
    # index: ?
    # data: [0x01]  // enable camera
    # length: 0x01
    # for index in range(0x00, 0xff):
    #     time.sleep(0.5)
    #     logger.warning(f"Trying index {index}")
    #     try:
    #         device.ctrl_transfer(0x21, 0x01, 0xd0d, index, [0x01], 0x01)
    #         break
    #     except usb.core.USBError as e:
    #         logger.error(e)
            # continue
    index = 0x0
    if device.is_kernel_driver_active(0):
        logger.info("Detaching kernel driver")
        device.detach_kernel_driver(0)
        if device.is_kernel_driver_active(0):
            logger.error("Failed to detach kernel driver")
            sys.exit(1)
        else:
            logger.info("Kernel driver detached")
    device.ctrl_transfer(0x21, 0x01, 0x606, index, [0x01])
    device.attach_kernel_driver(0)

import cv2
def main():
    if not usb.core.find(idVendor=device_vid, idProduct=device_pid):
        load_fw()
        wait_for_device()
    open_device()
    info = lcd_get_info()
    img = cv2.imread("lena.jpg")
    # resize the image to the LCD size, and convert to ARGB8888
    img = cv2.resize(img, (info["width"], info["height"]))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGBA)
    data = img.tobytes()
    lcd_xfer_image(info["width"], info["height"], data)

    # enable_camera()

if __name__ == "__main__":
    main()
