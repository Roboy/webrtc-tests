# WebRTC aiortc test

Adapted from example-server

## Install the dependencies

`pip install aiohttp aiortc uvloop`

## Modify aiortc to allow this usecase
Requires some in-place modifications to the aiortc library:  
* `rtcrtpsender.py`
  * `self.lastBitrateEstimate = 0` in `__init__` (eg. L.106)   
  *  in `_handle_rtcp_packet` (~ L.265) replace
     ```python
     if self.__encoder and hasattr(self.__encoder, "target_bitrate"):
         self.__encoder.target_bitrate = bitrate
     ```
     with
     ```python
     self.lastBitrateEstimate = bitrate
     if self.__encoder and hasattr(self.__encoder, "target_bitrate"):
         #self.__encoder.target_bitrate = bitrate
         pass
     ```
     to enable bitrate modification and value tracking
* `codecs/h264.py`
  * consider hacking this to adjust the `libx264` parameters:
  * in line ~135
  * consider disabling profile and level parameters
  * consider adding `"preset": "ultrafast",` to improve CPU encoding
  * consider adding `"bframes": "2",` to improve quality (last line)

Also install uvloop package to improve the performance a bit

run `server_video.py --play-from <videoFile>`