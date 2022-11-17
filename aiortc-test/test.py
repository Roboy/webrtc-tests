import fractions

import av

codec_name = "h264_nvenc"
MAX_FRAME_RATE = 30

av.codec.codec.dump_codecs()

codec = av.CodecContext.create(codec_name, "w")
codec.width = 1920
codec.height = 1080
codec.bit_rate = 1000000
codec.pix_fmt = "yuv420p"
codec.framerate = fractions.Fraction(MAX_FRAME_RATE, 1)
codec.time_base = fractions.Fraction(1, MAX_FRAME_RATE)
codec.options = {
    "profile": "baseline",
    "level": "31",
    "tune": "zerolatency",  # does nothing using h264_omx
}
codec.open()

print(str(codec))