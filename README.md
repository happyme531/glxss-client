# glxss-client
Open source client for llvision Glxss Me smart glasses


### 当前可用功能 (G25固件)

- 加载固件
- 显示屏显示指定画面
- 音频输入输出

### 已知不可用功能

- 摄像头。怀疑是固件不支持？
- 其它没实现的各种功能 (传感器，按键，...）

#### 注意：G26PRO固件的通信协议有很大不同


## 实现细节

- 眼镜上电后会识别到`03e7:2150 Intel Myriad VPU [Movidius Neural Compute Stick]`这个USB设备，是bootloader状态，此时屏幕不亮，需要加载固件
- G25固件加载后屏幕亮起，显示X图标，此时USB设备变为`2e09:0030 LLVISION Corporation LLVISION G25 Firmware`
  - 包含摄像头(UVC), 传感器(HID), 屏幕(vendor specific)和音频四个功能，一共6个interface
  - 摄像头虽然走的是UVC协议，可以被比较正常的识别，但似乎读不出图像数据。
  - 音频是标准协议，完全正常
- G26PRO固件加载后屏幕不亮，USB设备变为`2e09:0041 LLVision Corporation LLVision G26`
  - 摄像头不再显示是UVC协议了
  - 没有HID
