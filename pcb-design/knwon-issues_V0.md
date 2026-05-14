# HiveScale V0 breakout PCB - known Issues

The V0 PCB wires VIN on the ESP32. This does not work for most cheap boards as the VIN is directly connected to the DC/DC converter and meant for 5V in only.

- [ ] Preliminary fix: Remove the VIN PIN on the ESP32 and solder a wire from on the back of the PCB from VIN to 3V3.
<img src="![image-url](https://github.com/MacNite/HiveScale/blob/main/pcb-design/pictures/PCB_V0_Vin_fix_A.jpg)" alt="ESP32 front" width="300" height="200">
<img src="![image-url](https://github.com/MacNite/HiveScale/blob/main/pcb-design/pictures/PCB_V0_Vin_fix_B.jpg)" alt="ESP32 back" width="300" height="200">
<img src="![image-url](https://github.com/MacNite/HiveScale/blob/main/pcb-design/pictures/PCB_V0_Vin_fix_C.jpg)" alt="PCB back" width="300" height="200">