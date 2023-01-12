import argparse
import asyncio
import datetime
import fractions
import json
import logging
import os
import platform
import ssl
import uuid
from typing import Optional, Callable

import av.frame
from av.video.reformatter import VideoReformatter


logger = logging.getLogger("pc")
try:
    import uvloop

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ModuleNotFoundError as e:
    logger.warning("Could not find uvloop; installing it is recommended for performance improvements")

import aiortc.codecs.h264
# import cv2
from aiohttp import web
from av import VideoFrame

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, clock, RTCDataChannel
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder, MediaRelay

ROOT = os.path.dirname(__file__)

pcs = set()
relay = MediaRelay()
play_file = None

# Override bitrate parameters of h264
aiortc.codecs.h264.MIN_BITRATE = 100_000
aiortc.codecs.h264.MAX_BITRATE = 5_000_000



webcam_relay = None
webcam = None
mic_relay = None
mic = None


def create_webcam_track():
    global webcam_relay, webcam

    # 3840x2160
    # 1920x1080
    # 1280x720
    options = {"framerate": "30", "video_size": "1920x1080", "input_format": "mjpeg"}
    if webcam_relay is None:
        if platform.system() == "Darwin":
            webcam = MediaPlayer(
                "default:none", format="avfoundation", options=options
            )
        elif platform.system() == "Windows":
            webcam = MediaPlayer(
                "video=HD USB CAMERA", format="dshow", options=options
            )
        else:
            webcam = MediaPlayer("/dev/video0", format="v4l2", options=options)
        webcam_relay = MediaRelay()
    # buffered = false because we always want the latest image and rather drop frames if sending lags behind
    return webcam_relay.subscribe(webcam.video, False)

def create_mic_track():
    global mic_relay, mic
    
    options = {}
    if mic_relay is None:
        if platform.system() == "Darwin":
            return None
            pass
        elif platform.system() == "Windows":
            return None
            pass
        else:
            mic = MediaPlayer("default", format="pulse", options=options)
        mic_relay = MediaRelay()
    # buffered = false because we always want the latest image and rather drop frames if sending lags behind
    return mic.audio #mic_relay.subscribe(mic.audio, False)



class VideoReducerTrack(MediaStreamTrack):
    """
    A video stream track that reduces resolution and framerate of another video track
    """

    kind = "video"

    time_epsilon = 0.01
    """ Minimal delta that gets ignored for FPS-limits """

    def __init__(self, track: MediaStreamTrack, target_fps=30, target_height=1080):
        super().__init__()  # don't forget this!
        assert (track.kind == "video")
        self.track = track
        self.target_fps = target_fps
        self.target_height = target_height
        self.last_frame_time = 0
        # Pipelining: we run multiple reformatters in parallel (otherwise we segfault)
        self.__reformatter = [VideoReformatter(), VideoReformatter()]
        self.__next_reformatter = 0
        self.onFrameSent: Optional[Callable] = None
        self.__last_sent_frame_time: datetime.datetime = clock.current_datetime()
        self.__loop = asyncio.get_event_loop()
        self.__next_frame = None
        self.__recv_lock = asyncio.Lock()

    @staticmethod
    def round_next_2x(n):
        """ Scale number to next multiple of two since video encoders only allow for even pixel sizes """
        return int(round(float(n) / 2) * 2)

    def __reformat(self, frame, w: int, h: int, r=0):
        # Our Webcam provides video in mjpeg format but h264 etc encode yuv420p.
        # We use this to also do the colour space conversion to save time not doing that later on
        # causes ffmpeg to log a warning "deprecated pixel format used, make sure you did set range correctly",
        # but I was not able to teach it not to
        return self.__reformatter[r].reformat(frame, width=w, height=h, format="yuv420p", interpolation="FAST_BILINEAR")

    async def __prepare_next_frame(self):
        time_0 = clock.current_datetime()
        # This function can be called multiple times in parallel (pipelining of reformatting).
        # Make sure only one gets the latest frame
        async with self.__recv_lock:
            time_1 = clock.current_datetime()
            # Drop frames until the target framerate is achieved
            while True:
                frame = await self.track.recv()
                frame_time = frame.time
                if fractions.Fraction(1, self.target_fps) - (frame_time - self.last_frame_time) <= self.time_epsilon:
                    break
                # logger.info("dropped frame to keep target fps: " + str(fractions.Fraction(1, self.target_fps) - (frame_time - self.last_frame_time)))

            self.last_frame_time = frame_time

            # Get the next reformatter and swap so that we can run multiple in parallel
            r = self.__next_reformatter
            self.__next_reformatter = (self.__next_reformatter + 1) % len(self.__reformatter)

        time_2 = clock.current_datetime()
        # Scale the frame
        h = self.round_next_2x(min(self.target_height, frame.height))
        w = self.round_next_2x(float(h) / frame.height * frame.width)  # proportional
        # separate thread because this takes time
        new_frame = await self.__loop.run_in_executor(
            None, self.__reformat, frame, w, h, r
        )

        time_3 = clock.current_datetime()
        #logger.info("Prepare frame times: await lock: %i, receive: %i, reformat: %i",
        #            (time_1 - time_0).microseconds,
        #            (time_2 - time_1).microseconds,
        #            (time_3 - time_2).microseconds)

        return new_frame


    async def recv(self):
        time_1 = clock.current_datetime()

        if self.__next_frame is None:
            self.__next_frame = asyncio.ensure_future(self.__prepare_next_frame())

        # Do the swapparoo to "pipeline" frames so multiple frames can be reformatted in parallel
        next_frame = self.__next_frame

        # Already launch preparation of next frame so eg. reformat can run in parallel
        self.__next_frame = asyncio.ensure_future(self.__prepare_next_frame())

        # Only await the task after the next one has been started
        frame = await next_frame

        if self.onFrameSent:
            self.onFrameSent(frame)

        time_2 = clock.current_datetime()
        #logger.info("Frame times: encode/send: %i, fetch/wait reformat: %i",
        #            (time_1 - self.__last_sent_frame_time).microseconds,
        #            (time_2 - time_1).microseconds)
        self.__last_sent_frame_time = time_2

        return frame

    def stop(self) -> None:
        super().stop()
        self.track.stop()


async def index(request):
    content = open(os.path.join(ROOT, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def javascript(request):
    content = open(os.path.join(ROOT, "client.js"), "r").read()
    return web.Response(content_type="application/javascript", text=content)


async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pc_id = "PeerConnection(%s)" % uuid.uuid4()
    pcs.add(pc)

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)

    log_info("Created for %s", request.remote)

    # prepare local media
    player = None if play_file is None else MediaPlayer(play_file,
                                                        loop=True)  # os.path.join(ROOT, "demo-instruct.wav"))
    reduced_video_track: Optional[VideoReducerTrack] = None
    if args.record_to:
        recorder = None  # MediaRecorder(args.record_to)
    else:
        recorder = MediaBlackhole()

    video_sender = None

    target_bitrate = 1_000_000
    target_fps = 30
    target_height = 1080

    @pc.on("datachannel")
    def on_datachannel(channel: RTCDataChannel):

        stats_last_timestamp: datetime.datetime = clock.current_datetime()
        stats_last_bytecount = 0

        stats_last_frame_time: float = 0
        stats_latest_frame_time: float = 0
        stats_last_framecount = 0

        def h264_config_bitrate_at_fps(fps, bitrate):
            """ Scales the bitrate with the fps because h264 internally always uses MAX_FRAME_RATE to calculate the
            allowed bits per frame """
            return bitrate * aiortc.codecs.h264.MAX_FRAME_RATE / fps

        def on_frame_sent(frame: av.frame.Frame):
            nonlocal stats_last_framecount, stats_latest_frame_time
            stats_last_framecount += 1
            stats_latest_frame_time = frame.time
            channel.send("frame: " + str(frame.time))

        async def sendStats():
            nonlocal stats_last_bytecount, stats_last_timestamp, stats_last_framecount, stats_last_frame_time
            stats = await video_sender.getStats()
            sender_stats = stats["outbound-rtp_" + str(id(video_sender))]
            current_timestamp: datetime.datetime = sender_stats.timestamp
            current_bytecount = sender_stats.bytesSent
            current_bps = (current_bytecount - stats_last_bytecount) / (
                    current_timestamp - stats_last_timestamp).seconds
            encoder_name = str(video_sender._RTCRtpSender__encoder.__class__.__name__)
            if encoder_name == 'H264Encoder':
                encoder_name += ' / ' + str(video_sender._RTCRtpSender__encoder.codec.name)

            fps = stats_last_framecount / (stats_latest_frame_time - stats_last_frame_time)

            channel.send("stats " + json.dumps({
                "Codec": encoder_name,
                " Target FPS": str(reduced_video_track.target_fps),
                "Current FPS": str(fps),
                "Target Resolution": str(reduced_video_track.target_height) + 'p',
                "Est. Bandwidth": video_sender.lastBitrateEstimate / 1000 if hasattr(video_sender,
                                                                                     "lastBitrateEstimate") else 'n/a',
                "...Target kBit": target_bitrate / 1000,
                "fpsTarget kBit": video_sender._RTCRtpSender__encoder.target_bitrate / 1000,
                "..Current kBit": current_bps * 8 / 1000
            }))
            stats_last_timestamp = current_timestamp
            stats_last_bytecount = current_bytecount
            stats_last_frame_time = stats_latest_frame_time
            stats_last_framecount = 0

        reduced_video_track.onFrameSent = on_frame_sent

        async def loopmsg():
            while not channel.readyState == "open":
                await asyncio.sleep(0.1)
            channel.send('hello')
            await asyncio.sleep(3)
            while channel.readyState != "closed":
                for i in range(10000):
                #while channel.bufferedAmount < 10:
                    channel.send('test')
                await asyncio.sleep(20)
                #break

        #asyncio.ensure_future(loopmsg())

        #loopedidoop = asyncio.get_running_loop()
        #def loopmsg2():
        #    while not channel.readyState == "open":
        #        await
        #    channel.send('hello')
        #    while channel.readyState != "closed":
        #        #for i in range(200):
        #        while channel.bufferedAmount < 10:
        #            channel.send('test')
        #        await asyncio.sleep(0)
        #asyncio.ensure_future(loopedidoop.run_in_executor(None, loopmsg2))

        @channel.on("message")
        async def on_message(message):
            nonlocal target_height, target_fps, target_bitrate
            if isinstance(message, str) and message.startswith("ping"):
                time = int(message[4:])
                logger.info('Receive delay: %i' % int(clock.current_datetime().timestamp()*1000 - time))
                channel.send("pong" + message[4:])
                # stat = await video_sender.getStats()
                try:
                    # channel.send("vcodec is " + str(video_sender._RTCRtpSender__encoder or "") + " / " + str(video_sender._RTCRtpSender__encoder.codec or ""))
                    await sendStats()
                    pass
                except Exception as e:
                    logging.error(e)
            if isinstance(message, str) and message.startswith("target_bitrate"):
                try:
                    target_bitrate = int(message[14:])
                    video_sender._RTCRtpSender__encoder.target_bitrate = h264_config_bitrate_at_fps(
                        reduced_video_track.target_fps, target_bitrate)
                    channel.send("new bitrate target is " + str(target_bitrate) + " / " + str(
                        video_sender._RTCRtpSender__encoder.target_bitrate))
                except Exception as e:
                    logging.error(e)
            if isinstance(message, str) and message.startswith("target_fps"):
                try:
                    target_fps = int(message[10:])
                    reduced_video_track.target_fps = target_fps
                    video_sender._RTCRtpSender__encoder.target_bitrate = h264_config_bitrate_at_fps(
                        reduced_video_track.target_fps, target_bitrate)
                    channel.send("new fps target is " + str(target_fps))
                except Exception as e:
                    logging.error(e)
            if isinstance(message, str) and message.startswith("target_height"):
                try:
                    target_height = int(message[13:])
                    reduced_video_track.target_height = target_height
                    channel.send("new pixel height target is " + str(target_height))
                except Exception as e:
                    logging.error(e)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        log_info("Connection state is %s", pc.connectionState)

        if pc.connectionState == "connected":
            pass

        if pc.connectionState == "failed" or pc.connectionState == "closed":
            logger.info('Closing connection')
            await pc.close()
            pcs.discard(pc)

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        if track.kind == "audio":
            # pc.addTrack(player.audio)
            if recorder is not None:
                recorder.addTrack(track)
        elif track.kind == "video":
            # pc.addTrack(
            #     VideoTransformTrack(
            #         relay.subscribe(track), transform=params["video_transform"]
            #     )
            # )
            if args.record_to and recorder is not None:
                recorder.addTrack(relay.subscribe(track))

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)
            if recorder is not None:
                await recorder.stop()

    # handle offer
    await pc.setRemoteDescription(offer)
    if recorder is not None:
        await recorder.start()

    if player and player.audio:
        pc.addTrack(player.audio)

    if player and player.video:
        reduced_video_track = VideoReducerTrack(player.video)
        video_sender = pc.addTrack(reduced_video_track)
    else:
        reduced_video_track = VideoReducerTrack(create_webcam_track())
        video_sender = pc.addTrack(reduced_video_track)
        mic_track = create_mic_track()
        if mic_track:
            pc.addTrack(mic_track)

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        ),
    )


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WebRTC audio / video / data-channels demo"
    )
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--record-to", help="Write received media to a file."),
    parser.add_argument("--play-from", help="Read the media from a file and sent it."),
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    av.logging.set_level(av.logging.ERROR)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    # create media source
    if args.play_from:
        play_file = args.play_from
        # logger.info("Playing %s", args.play_from)
    else:
        play_file = None

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/client.js", javascript)
    app.router.add_post("/offer", offer)
    web.run_app(
        app, access_log=None, host=args.host, port=args.port, ssl_context=ssl_context
    )
