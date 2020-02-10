#!/usr/bin/env python
# encoding: utf-8
"""
@author: Shanda Lau 刘祥德
@license: (C) Copyright 2019-now, Node Supply Chain Manager Corporation Limited.
@contact: shandalaulv@gmail.com
@software: 
@file: manager.py
@time: 2019/11/16 13:22
@version 1.0
@desc:
"""

import os.path as osp
import sys
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from enum import Enum
from multiprocessing import cpu_count
from typing import List

import stream
from detection.params import DispatchBlock, ConstructResult, BlockInfo, ConstructParams, DetectorParams
from stream.websocket import *
from utils import NoDaemonPool as Pool
from .capture import *
from .component import stream_pipes
from .detect_funcs import detect_based_task
from .detector import *


# from pynput.keyboard import Key, Controller, Listener
# import keyboard
# from capture import *


class MonitorType(Enum):
    PROCESS_BASED = 1,
    THREAD_BASED = 2,
    PROCESS_THREAD_BASED = 3
    RAY_BASED = 4
    TASK_BASED = 5


# Monitor will build multiple video stream receivers according the video configuration
class DetectionMonitor(object):

    def __init__(self, cfgs: Path, stream_path: Path, sample_path: Path, frame_path: Path,
                 region_path: Path,
                 offline_path: Path = None, build_pool=True) -> None:
        super().__init__()
        # self.cfgs = I.load_video_config(cfgs)[-1:]
        # self.cfgs = I.load_video_config(cfgs)
        # self.cfgs = [c for c in self.cfgs if c.enable]
        # self.cfgs = [c for c in cfgs if enable_options[c.index]]
        self.cfgs = cfgs
        self.quit = False
        # Communication Pipe between detector and stream receiver
        self.pipes = [Manager().Queue(c.max_streams_cache) for c in self.cfgs]
        time_stamp = generate_time_stamp()
        self.stream_path = stream_path / time_stamp
        self.sample_path = sample_path / time_stamp
        self.frame_path = frame_path / time_stamp
        self.region_path = region_path / time_stamp
        self.offline_path = offline_path
        self.process_pool = None
        self.thread_pool = None
        self.shut_down_event = Manager().Event()
        self.shut_down_event.clear()
        if build_pool:
            self.process_pool = Pool(processes=cpu_count() - 1)
            self.thread_pool = ThreadPoolExecutor()
        self.clean()
        self.stream_receivers = [
            stream.StreamReceiver(self.stream_path / str(c.index), offline_path, c, self.pipes[idx]) for idx, c in
            enumerate(self.cfgs)]

    def monitor(self):
        self.call()
        self.wait()

    # def set_runtime(self, runtime):
    #     self.runtime = runtime

    def shut_down_from_keyboard(self):
        logger.info('Click Double Enter to shut down system.')
        while True and not self.shut_down_event.is_set():
            c = sys.stdin.read(1)
            logger.info(c)
            if c == '\n':
                self.notify_shut_down()
                break
        # if keycode == Key.enter:
        #     self.shut_down_event.set()

    def shut_down_after(self, runtime=-1):
        if runtime == -1:
            return
        logger.info('System will exit after [{}] seconds'.format(runtime))
        threading.Timer(runtime, self.notify_shut_down).start()

    def notify_shut_down(self):
        if not self.shut_down_event.is_set():
            self.shut_down_event.set()

    def listen(self):
        # Listener(on_press=self.shut_down_from_keyboard).start()
        threading.Thread(target=self.shut_down_from_keyboard, daemon=True).start()
        logger.info('*******************************Monitor: Listening exit event********************************')
        # if self.runtime != -1:
        #     time.sleep(self.runtime)
        # else:
        #     input('')
        self.shut_down_event.wait()
        logger.info('*******************************Monitor: preparing exit system********************************')
        self.cancel()

    def cancel(self):
        pass

    def call(self):
        for i, cfg in enumerate(self.cfgs):
            # clean all legacy streams and candidates files before initialization
            self.init_stream_receiver(i)
            self.init_detection(cfg, i)

    def wait(self):
        logger.info('Wait processes done.')
        if self.process_pool is not None:
            self.process_pool.close()
            self.process_pool.join()

        logger.info('Closed Pool')

    def init_detection(self, cfg, i):
        if self.process_pool is not None:
            self.process_pool.apply_async(detect,
                                          (self.stream_path / str(cfg.index), self.region_path / str(cfg.index),
                                           self.pipes[i], cfg,))

    # def init_stream_receiver(self, cfg, i):
    #     self.process_pool.apply_async(I.read_stream, (self.stream_path / str(cfg.index), cfg, self.pipes[i],))
    def init_stream_receiver(self, i):
        if self.process_pool is not None:
            return self.process_pool.apply_async(self.stream_receivers[i].receive_online)

    def clean(self):
        clean_dir(self.sample_path)
        clean_dir(self.stream_path)
        clean_dir(self.region_path)


# Base class embedded controllers of detector
# Each video has a detector controller
# But a controller will manager [row*col] concurrency threads or processes
# row and col are definied in video configuration
class EmbeddingControlMonitor(DetectionMonitor):
    def __init__(self, cfgs: Path, stream_path: Path, sample_path: Path, frame_path: Path, region_path,
                 offline_path: Path = None, build_pool=True) -> None:
        super().__init__(cfgs, stream_path, sample_path, frame_path, region_path, offline_path, build_pool)
        self.caps_queue = [Manager().Queue(maxsize=500) for c in self.cfgs]
        self.msg_queue = [Manager().Queue(maxsize=500) for c in self.cfgs]
        self.caps = []
        self.controllers = []

    def init_caps(self):
        for idx, c in enumerate(self.cfgs):
            if c.online == "http":
                self.init_http_caps(c, idx)
            elif c.online == "rtsp":
                self.init_rtsp_caps(c, idx)
            else:
                self.init_offline_caps(c, idx)

    def init_offline_caps(self, c, idx):
        self.caps.append(
            VideoOfflineCapture(self.stream_path / str(c.index), self.sample_path / str(c.index),
                                self.offline_path / str(c.index),
                                self.pipes[idx],
                                self.caps_queue[idx], c, idx, c.sample_rate,
                                delete_post=False))

    def init_http_caps(self, c, idx):
        self.caps.append(
            VideoOnlineSampleCapture(self.stream_path / str(c.index), self.sample_path / str(c.index),
                                     self.pipes[idx],
                                     self.caps_queue[idx],
                                     c, idx, c.sample_rate))

    def init_rtsp_caps(self, c, idx):
        self.caps.append(
            VideoRtspCapture(self.stream_path / str(c.index), self.sample_path / str(c.index),
                             self.pipes[idx], self.caps_queue[idx], c, idx, c.sample_rate)
        )

    def call(self):
        self.init_caps()
        # Init stream receiver firstly, ensures video index that is arrived before detectors begin detection..
        for i, cfg in enumerate(self.cfgs):
            res = self.init_stream_receiver(i)
            # logger.debug(res.get())

        # Run video capture from stream
        for i in range(len(self.cfgs)):
            self.caps[i].read()

        # Init detector controller
        self.init_controllers()

    def init_controllers(self):
        pass

        # Concurrency based multi processes


class EmbeddingControlBasedProcessMonitor(EmbeddingControlMonitor):

    def __init__(self, cfgs: Path, stream_path: Path, sample_path, frame_path, region_path: Path,
                 offline_path: Path = None) -> None:
        super().__init__(cfgs, stream_path, sample_path, frame_path, region_path, offline_path)

    def init_controllers(self):
        self.controllers = [
            ProcessBasedDetectorController(cfg, self.stream_path / str(cfg.index), self.region_path / str(cfg.index),
                                           self.frame_path / str(cfg.index),
                                           self.caps_queue[idx],
                                           self.pipes[idx]
                                           ) for
            idx, cfg in enumerate(self.cfgs)]
        for i, cfg in enumerate(self.cfgs):
            logger.info('Init detector controller [{}]....'.format(cfg.index))
            res, detect_proc = self.controllers[i].start(self.process_pool)
            res.get()
            logger.info('Done init detector controller [{}]....'.format(cfg.index))


class EmbeddingControlBasedTaskMonitor(EmbeddingControlMonitor):

    def __init__(self, cfgs, scfg, model, stream_path: Path, sample_path, frame_path, region_path: Path,
                 offline_path: Path = None) -> None:
        super().__init__(cfgs, stream_path, sample_path, frame_path, region_path, offline_path)
        self.model = model
        self.scfg = scfg
        self.task_futures = []
        # self.process_pool = None
        # self.thread_pool = None

    def init_controllers(self):
        self.controllers = [
            TaskBasedDetectorController(cfg, self.stream_path / str(cfg.index), self.region_path / str(cfg.index),
                                        self.frame_path / str(cfg.index),
                                        self.caps_queue[idx],
                                        self.pipes[idx],
                                        self.msg_queue[idx]
                                        ) for
            idx, cfg in enumerate(self.cfgs)]
        for i, cfg in enumerate(self.cfgs):
            logger.info('Init detector controller [{}]....'.format(cfg.index))
            # self.task_futures.append(self.controllers[i].start(self.thread_pool))
            self.controllers[i].start(self.thread_pool)
            logger.info('Done init detector controller [{}]....'.format(cfg.index))

    def init_rtsp_caps(self, c, idx):
        self.caps.append(
            VideoRtspCallbackCapture(self.stream_path / str(c.index), self.sample_path / str(c.index),
                                     self.pipes[idx], self.caps_queue[idx], c, idx, self.controllers[idx],
                                     c.sample_rate)
        )

    def init_offline_caps(self, c, idx):
        self.caps.append(
            VideoOfflineCallbackCapture(self.stream_path / str(c.index), self.sample_path / str(c.index),
                                        self.offline_path / str(c.index),
                                        self.pipes[idx],
                                        self.caps_queue[idx], c, idx, self.controllers[idx], self.shut_down_event,
                                        c.sample_rate,
                                        delete_post=False))

    def cancel(self):
        for idx, cap in enumerate(self.caps):
            cap.quit.set()
            self.controllers[idx].quit.set()

    def init_websocket_clients(self):
        for idx, cfg in enumerate(self.cfgs):
            threading.Thread(target=websocket_client, daemon=True,
                             args=(self.msg_queue[idx], cfg, self.scfg)).start()
            logger.info(f'Controller [{cfg.index}]: Websocket client [{cfg.index}] is initializing...')
            # asyncio.get_running_loop().run_until_complete(websocket_client_async(self.msg_queue[idx]))

    def call(self):
        self.init_websocket_clients()
        # Init detector controller
        self.init_controllers()
        self.init_caps()
        # Init stream receiver firstly, ensures video index that is arrived before detectors begin detection..
        for i, cfg in enumerate(self.cfgs):
            res = self.init_stream_receiver(i)
            # logger.debug(res.get())
        # Run video capture from stream
        for i in range(len(self.cfgs)):
            if self.process_pool is not None:
                self.task_futures.append(self.process_pool.apply_async(self.caps[i].read, (self.model,)))
                self.task_futures[-1].get()

    def wait(self):
        if self.process_pool is not None:
            try:
                # self.listen()
                # logger.info('Waiting processes canceled.')
                self.listen()
                for idx, r in enumerate(self.task_futures):
                    if r is not None:
                        logger.info(
                            '*******************************Controller [{}]: waiting process canceled********************************'.format(
                                self.cfgs[idx].index))
                        r.get()
                        logger.info(
                            '*******************************Controller [{}]: exit********************************'.format(
                                self.cfgs[idx].index))
                # results = [r.get() for r in self.task_futures if r is not None]
                self.process_pool.close()
                self.process_pool.join()
            except:
                self.process_pool.terminate()


class EmbeddingControlBasedThreadMonitor(EmbeddingControlMonitor):

    def __init__(self, cfgs: Path, stream_path: Path, sample_path, frame_path, region_path: Path,
                 offline_path: Path = None) -> None:
        super().__init__(cfgs, stream_path, sample_path, frame_path, region_path, offline_path)

    def init_controllers(self):
        self.controllers = [
            ProcessBasedDetectorController(cfg, self.stream_path / str(cfg.index), self.region_path / str(cfg.index),
                                           self.frame_path / str(cfg.index),
                                           self.caps_queue[idx],
                                           self.pipes[idx]
                                           ) for
            idx, cfg in enumerate(self.cfgs)]
        for i, cfg in enumerate(self.cfgs):
            logger.info('Init detector controller [{}]....'.format(cfg.index))
            self.thread_res = self.controllers[i].start(self.thread_pool)
            logger.info('Done init detector controller [{}]....'.format(cfg.index))

    # Concurrency based multiple threads and multiple processes


class EmbeddingControlBasedThreadAndProcessMonitor(EmbeddingControlMonitor):

    def __init__(self, cfgs: Path, stream_path: Path, sample_path: Path, frame_path: Path,
                 region_path: Path,
                 offline_path=None) -> None:
        super().__init__(cfgs, stream_path, sample_path, frame_path, region_path, offline_path)

    def init_controllers(self):
        self.controllers = [
            ProcessAndThreadBasedDetectorController(cfg, self.stream_path / str(cfg.index),
                                                    self.region_path / str(cfg.index),
                                                    self.frame_path / str(cfg.index),
                                                    self.caps_queue[idx],
                                                    self.pipes[idx]
                                                    ) for
            idx, cfg in enumerate(self.cfgs)]
        for i, cfg in enumerate(self.cfgs):
            logger.info('Init detector controller [{}]....'.format(cfg.index))
            pool_res, thread_res = self.controllers[i].start([self.process_pool, self.thread_pool])
            self.thread_res = thread_res
            # logger.info(res.get())
            logger.info('Done init detector controller [{}]....'.format(cfg.index))

    def wait(self):
        super().wait()
        # wait all threads canceled in Thread Pool Executor
        wait(self.thread_res, return_when=ALL_COMPLETED)


class DetectorController(object):
    def __init__(self, cfg: VideoConfig, stream_path: Path, candidate_path: Path, frame_path: Path,
                 frame_queue: Queue,
                 index_pool: Queue,
                 msg_queue: Queue) -> None:
        super().__init__()
        self.cfg = cfg
        self.stream_path = stream_path
        self.frame_path = frame_path
        self.candidate_path = candidate_path
        self.block_path = candidate_path / 'blocks'
        self.result_path = self.candidate_path / 'frames'
        self.crop_result_path = self.candidate_path / 'crops'
        self.rect_stream_path = self.candidate_path / 'render-streams'
        self.original_stream_path = self.candidate_path / 'original-streams'
        self.test_path = self.candidate_path / 'tests'
        self.create_workspace()

        self.result_cnt = 0
        self.stream_cnt = 0
        # self.process_pool = process_pool
        self.x_num = cfg.routine['col']
        self.y_num = cfg.routine['row']
        self.x_step = 0
        self.y_step = 0
        self.block_info = BlockInfo(self.y_num, self.x_num, self.y_step, self.x_step)

        # self.send_pipes = [Manager().Queue() for i in range(self.x_num * self.y_num)]
        # self.receive_pipes = [Manager().Queue() for i in range(self.x_num * self.y_num)]
        self.pipe = stream_pipes[self.cfg.index]
        self.index_pool = index_pool
        self.frame_queue = frame_queue
        self.msg_queue = msg_queue
        self.result_queue = Manager().Queue(self.cfg.max_streams_cache)
        self.quit = Manager().Event()
        self.quit.clear()
        self.status = Manager().Value('i', SystemStatus.SHUT_DOWN)
        self.frame_cnt = Manager().Value('i', 0)
        # self.frame_cnt =
        self.next_prepare_event = Manager().Event()
        self.next_prepare_event.clear()
        self.pre_detect_index = -self.cfg.future_frames
        self.history_write = False
        self.original_frame_cache = Manager().dict()
        self.render_frame_cache = Manager().dict()
        self.history_frame_deque = Manager().list()
        self.render_rect_cache = Manager().dict()
        self.detect_index = Manager().dict()
        self.record_cnt = 48
        self.render_task_cnt = 0
        self.construct_cnt = 0
        self.dispatch_cnt = 0

        # time scheduler to clear cache
        self.render_events = Manager().dict()
        self.last_detection = -1
        self.continuous_filter_flag = False
        self.runtime = time.time()
        # self.clear_point = 0
        # self.fourcc = cv2.VideoWriter_fourcc(*'avc1')
        self.stream_render = DetectionStreamRender(self.cfg, 0, self.cfg.future_frames, self.msg_queue, self)
        self.LOG_PREFIX = f'Controller [{self.cfg.index}]: '
        self.save_cache = {}

        # def __getstate__(self):

    #     self_dict = self.__dict__.copy()
    #     del self_dict['process_pool']
    #     return self_dict
    #
    # def __setstate__(self, state):
    #     self.__dict__.update(state)

    def listen(self):
        if self.quit.wait():
            self.status.set(SystemStatus.SHUT_DOWN)
            self.stream_render.quit.set()

    def cancel(self):
        pass

    def clear_original_cache(self):
        len_cache = len(self.original_frame_cache)
        if len_cache > 1000:
            # original_head = self.original_frame_cache.keys()[0]
            thread = threading.Thread(
                target=clear_cache,
                args=(self.original_frame_cache,), daemon=True)
            # self.clear_cache(self.original_frame_cache)
            thread.start()
            logger.info(
                'Clear half original frame caches.')

    def clear_render_cache(self):
        # last_detect_internal = time.time() - self.last_detection
        # time_thresh = self.cfg.future_frames * 1.5 * 3
        # if last_detect_internal > time_thresh and len(self.render_frame_cache) > 500:
        if len(self.render_frame_cache) > 500:
            thread = threading.Thread(
                target=clear_cache,
                args=(self.render_frame_cache,), daemon=True)
            thread.start()
            logger.info('Clear half render frame caches')
        if len(self.render_rect_cache) > 500:
            thread = threading.Thread(
                target=clear_cache,
                args=(self.render_rect_cache,), daemon=True)
            thread.start()

    def init_control_range(self):
        # read a frame, record frame size before running detectors
        frame = self.frame_queue.get()
        frame, original_frame = preprocess(frame, self.cfg)
        self.x_step = int(frame.shape[1] / self.x_num)
        self.y_step = int(frame.shape[0] / self.y_num)
        self.block_info = BlockInfo(self.y_num, self.x_num, self.y_step, self.x_step)

    def init_detectors(self):
        logger.info('Init total [{}] detectors....'.format(self.x_num * self.y_num))
        self.detectors = []
        for i in range(self.x_num):
            for j in range(self.y_num):
                region_detector_path = self.block_path / (str(i) + '-' + str(j))
                index = self.x_num * i + j
                self.detectors.append(
                    Detector(self.x_step, self.y_step, i, j, self.cfg, self.send_pipes[index],
                             self.receive_pipes[index],
                             region_detector_path))
        logger.info('Detectors init done....')

    def create_workspace(self):

        self.rect_stream_path.mkdir(exist_ok=True, parents=True)
        self.result_path.mkdir(exist_ok=True, parents=True)
        self.crop_result_path.mkdir(exist_ok=True, parents=True)
        self.original_stream_path.mkdir(exist_ok=True, parents=True)
        self.block_path.mkdir(exist_ok=True, parents=True)
        self.test_path.mkdir(exist_ok=True, parents=True)

    # def start(self):
    #     self.process_pool.apply_async(self.control, (self,))

    def start(self, pool):
        self.status.set(SystemStatus.RUNNING)
        self.init_control_range()
        self.init_detectors()
        return None

    def write_frame_work(self):
        logger.info(
            '*******************************Controler [{}]: Init detection frame frame routine********************************'.format(
                self.cfg.index))
        current_time = time.strftime('%m-%d-%H-%M-', time.localtime(time.time()))
        target = self.result_path / (current_time + str(self.result_cnt) + '.png')
        logger.info('Writing stream frame into: [{}]'.format(str(target)))
        while True:
            if self.status.get() == SystemStatus.SHUT_DOWN and self.result_queue.empty():
                logger.info(
                    '*******************************Controller [{}]: Frame write routine exit********************************'.format(
                        self.cfg.index))
                break
            try:
                # r = self.get_result_from_queue()
                if not self.result_queue.empty():
                    result_queue = self.result_queue.get(timeout=1)
                    r, rects = result_queue[0], result_queue[2]
                    self.result_cnt += 1
                    current_time = time.strftime('%m-%d-%H-%M-', time.localtime(time.time()))
                    img_name = current_time + str(self.result_cnt) + '.png'
                    target = self.result_path / img_name
                    cv2.imwrite(str(target), r)
                    self.label_crop(r, img_name, rects)
                    self.save_bbox(img_name, rects)
            except Exception as e:
                logger.error(e)
        return True

    def get_result_from_queue(self):
        return self.result_queue.get(timeout=2)

    def collect_and_reconstruct(self, args, pool):
        logger.info('Controller [{}] start collect and construct'.format(self.cfg.index))
        cnt = 0
        start = time.time()
        while True:
            if self.status.get() == SystemStatus.SHUT_DOWN:
                break
            # logger.debug('Collecting sub-frames into a original frame....')
            # start = time.time()
            results = self.collect(args)
            # logger.info('Collect consume [{}]'.format(time.time() - start ))
            construct_result: ConstructResult = self.construct(results)
            # logger.debug('Done Construct sub-frames into a original frame....')
            cnt += 1
            if (cnt * self.cfg.sample_rate) % 100 == 0:
                end = time.time() - start
                logger.info(
                    'Detection controller [{}]: Operation Speed Rate [{}]s/100fs, unit process rate: [{}]s/f'.format(
                        self.cfg.index, round(end, 2), round(end / 100, 2)))
                start = time.time()
                cnt = 0
            frame = construct_result.frame
            if self.cfg.draw_boundary:
                frame, _ = preprocess(construct_result.frame, self.cfg)
                frame = draw_boundary(frame, self.block_info)
            # logger.info('Done constructing of sub-frames into a original frame....')
            if self.cfg.show_window:
                cv2.imshow('Reconstructed Frame', frame)
                cv2.waitKey(1)
        return True

    def dispatch(self):
        # start = time.time()
        while True:
            if self.status.get() == SystemStatus.SHUT_DOWN:
                break
            frame = self.frame_queue.get()
            self.frame_cnt.set(self.frame_cnt.get() + 1)
            self.original_frame_cache[self.frame_cnt.get()] = frame
            # self.render_frame_cache[self.frame_cnt.get()] = frame
            # logger.info(self.original_frame_cache.keys())
            frame, original_frame = preprocess(frame, self.cfg)
            if self.frame_cnt.get() % self.cfg.sample_rate == 0:
                logger.info('Dispatch frame to all detectors....')
                for idx, sp in enumerate(self.send_pipes):
                    sp.put(DispatchBlock(crop_by_se(frame, self.detectors[idx].start, self.detectors[idx].end),
                                         self.frame_cnt.get(), original_frame.shape))

            self.clear_original_cache()
            # internal = (time.time() - start) / 60
            # if int(internal) == self.cfg.sample_internal:
            #     cv2.imwrite(str(self.frame_path / ))
        return True

    def collect(self, args):
        res = []
        for rp in self.receive_pipes:
            res.append(rp.get())
        logger.info('Collect sub-frames from all detectors....')
        return res

    def construct_rgb(self, sub_frames):
        sub_frames = np.array(sub_frames)
        sub_frames = np.reshape(sub_frames, (self.x_num, self.y_num, self.x_step, self.y_step, 3))
        sub_frames = np.transpose(sub_frames, (0, 2, 1, 3, 4))
        constructed_frame = np.reshape(sub_frames, (self.x_num * self.x_step, self.y_num * self.y_step, 3))
        return constructed_frame

    def construct_gray(self, sub_frames):
        sub_frames = np.array(sub_frames)
        sub_frames = np.reshape(sub_frames, (self.y_num, self.x_num, self.y_step, self.x_step))
        sub_frames = np.transpose(sub_frames, (0, 2, 1, 3))
        constructed_frame = np.reshape(sub_frames, (self.y_num * self.y_step, self.x_num * self.x_step))
        return constructed_frame

    def save_bbox(self, frame_name, boundary_rect):
        bbox_path = str(self.candidate_path / 'bbox.json')
        self.save_cache[frame_name] = boundary_rect

        if not osp.exists(bbox_path):
            fw = open(bbox_path, 'w')
            fw.write(json.dumps(self.save_cache, indent=4))
            fw.close()

        if len(self.save_cache) == 2:
            fr = open(bbox_path, 'r')
            save_file = json.load(fr)
            fr.close()

            for key in self.save_cache:
                save_file[key] = self.save_cache[key]

            fw = open(bbox_path, 'w')
            fw.write(json.dumps(save_file, indent=4))
            fw.close()

            self.save_cache = {}

    def construct(self, *args):
        pass

    def label_crop(self, frame, label_name, rects):
        # label_w, label_h = 224, 224
        crop_path = self.crop_result_path / label_name
        # # p1 = (rect[0] - 80, rect[1] - 80)
        # # p2 = (rect[0] + 100, rect[1] + 100)
        # center_x, center_y = round(rects[0][0] + rects[0][2] / 2), round(rects[0][1] + rects[0][3] / 2)
        # start_x, start_y = round(center_x - label_w / 2), round(center_y - label_h / 2)
        # end_x = start_x + label_w
        # end_y = start_y + label_h
        # # start_x, start_y = rects[0][0] - 77, rects[0][1] - 77
        # # end_x = rects[0][0] + 97
        # # end_y = rects[0][1] + 97
        # if start_x < 0:
        #     start_x = 0
        # if start_y < 0:
        #     start_y = 0
        # if end_x > shape[1]:
        #     end_x = shape[1]
        # if end_y > shape[0]:
        #     end_y = shape[0]
        cropped = crop_by_rect(self.cfg, rects[0], frame)
        cv2.imwrite(str(crop_path), cropped)


class DetectionStreamRender(object):

    def __init__(self, cfg, detect_index, future_frames, msg_queue: Queue, controller: DetectorController) -> None:
        super().__init__()
        self.cfg = cfg
        self.detect_index = detect_index
        self.rect_stream_path = controller.rect_stream_path
        self.original_stream_path = controller.original_stream_path
        self.stream_cnt = 0
        self.index = controller.cfg.index
        self.is_trigger_write = False
        self.write_done = False
        self.controller = controller
        self.future_frames = future_frames
        self.sample_rate = controller.cfg.sample_rate
        self.render_frame_cache = controller.render_frame_cache
        self.render_rect_cache = controller.render_rect_cache
        self.original_frame_cache = controller.original_frame_cache
        self.next_prepare_event = Manager().Event()
        self.next_prepare_event.set()
        self.msg_queue = msg_queue
        # self.fourcc = cv2.VideoWriter_fourcc(*'avc1')
        self.fourcc = cv2.VideoWriter_fourcc(*'MP4V')
        self.quit = Manager().Event()
        self.quit.clear()
        self.status = Manager().Value('i', SystemStatus.RUNNING)
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        if self.quit.wait():
            self.next_prepare_event.set()
            self.status.set(SystemStatus.SHUT_DOWN)

    def next_st(self, detect_index):
        if detect_index - self.detect_index == self.future_frames:
            return detect_index
        else:
            return -1

    def is_window_reach(self, detect_index):
        return detect_index - self.detect_index > self.future_frames

    def reset(self, detect_index):
        if self.is_window_reach(detect_index):
            self.detect_index = detect_index
            self.is_trigger_write = False
            self.write_done = False
            self.next_prepare_event.set()
            logger.info('Reset stream render')

    def notify(self, current_index):
        # next_detect_stream_occurred = current_index - self.detect_index >= self.future_frames \
        #                               and not self.is_trigger_write
        if not self.is_trigger_write:
            if self.next_prepare_event.is_set():
                self.next_prepare_event.clear()
                # begin task asynchronously  in case blocking collector
                self.render_task(current_index, self.render_frame_cache, self.render_rect_cache,
                                 self.original_frame_cache)
                self.is_trigger_write = True
        if current_index - self.detect_index >= self.future_frames and self.write_done:
            # notify render task that the future frames(2s default required) are done
            if not self.next_prepare_event.is_set():
                self.next_prepare_event.set()
                logger.info(
                    f'Notify detection stream writer.Current frame index [{current_index}],Previous detected frame index [{self.detect_index}]...')

    def write_render_video_work(self, video_write, next_cnt, end_cnt, render_cache, rect_cache, frame_cache):
        if next_cnt < 1:
            next_cnt = 1
        start = time.time()
        try_times = 0
        while next_cnt < end_cnt:
            try:
                if self.status.get() == SystemStatus.SHUT_DOWN:
                    logger.info(
                        f'Video Render [{self.index}]: render task interruped by exit signal')
                    return next_cnt
                if next_cnt in render_cache:
                    forward_cnt = next_cnt + self.sample_rate
                    if forward_cnt > end_cnt:
                        forward_cnt = end_cnt
                    while forward_cnt > next_cnt:
                        if forward_cnt in render_cache:
                            break
                        forward_cnt -= 1
                    if forward_cnt - next_cnt <= 1:
                        video_write.write(render_cache[next_cnt])
                        next_cnt += 1
                    elif forward_cnt - next_cnt > 1:
                        step = forward_cnt - next_cnt
                        first_rects = rect_cache[next_cnt]
                        last_rects = rect_cache[forward_cnt]
                        if len(last_rects) != len(first_rects):
                            next_cnt += 1
                            continue
                        for i in range(step):
                            draw_flag = True
                            for j in range(min(len(first_rects), len(last_rects))):
                                first_rect = first_rects[j]
                                last_rect = last_rects[j]
                                delta_x = (last_rect[0] - first_rect[0]) / step
                                delta_y = (last_rect[1] - first_rect[1]) / step
                                if abs(delta_x) > 100 / step or abs(delta_y) > 100 / step:
                                    draw_flag = False
                                    break
                                color = np.random.randint(0, 255, size=(3,))
                                color = [int(c) for c in color]
                                # p1 = (first_rect[0] + int(delta_x * i) - 80, first_rect[1] + int(delta_y * i) - 80)
                                # p2 = (first_rect[0] + int(delta_x * i) + 100, first_rect[1] + int(delta_y * i) + 100)
                                frame = frame_cache[next_cnt]
                                p1, p2 = bbox_points(self.cfg, first_rect, frame.shape, int(delta_x), int(delta_y))
                                cv2.rectangle(frame, p1, p2, color, 2)
                            if not draw_flag:
                                frame = frame_cache[next_cnt]
                            video_write.write(frame)
                            next_cnt += 1
                elif next_cnt in frame_cache:
                    video_write.write(frame_cache[next_cnt])
                    next_cnt += 1
                else:
                    try_times += 1
                    time.sleep(0.5)
                    if try_times > 100:
                        try_times = 0
                        logger.info(f'Try time overflow.round to the next cnt: [{try_times}]')
                        next_cnt += 1
                    logger.info(f'Lost frame index: [{next_cnt}]')

                end = time.time()
                if end - start > 30:
                    logger.info('Task time overflow, complete previous render task.')
                    break
            except Exception as e:
                if end - start > 30:
                    logger.info('Task time overflow, complete previous render task.')
                    break
                logger.error(e)
        return next_cnt

    def write_original_video_work(self, video_write, next_cnt, end_cnt, frame_cache):
        if next_cnt < 1:
            next_cnt = 1
        start = time.time()
        try_times = 0
        while next_cnt < end_cnt:
            try:
                if self.status.get() == SystemStatus.SHUT_DOWN:
                    logger.info(
                        f'Video Render [{self.index}]: original task interruped by exit signal')
                    return False
                if next_cnt in frame_cache:
                    video_write.write(frame_cache[next_cnt])
                    next_cnt += 1
                else:
                    try_times += 1
                    time.sleep(0.5)
                    if try_times > 100:
                        try_times = 0
                        logger.info(f'Try time overflow.round to the next cnt: [{try_times}]')
                        next_cnt += 1
                    logger.info(f'Lost frame index: [{next_cnt}]')

                end = time.time()
                if end - start > 30:
                    logger.info('Task time overflow, complete previous render task.')
                    break
            except Exception as e:
                if end - start > 30:
                    logger.info('Task time overflow, complete previous render task.')
                    break
                logger.error(e)
        return next_cnt

    def render_task(self, current_idx, render_cache, rect_cache, frame_cache):
        current_time = time.strftime('%m-%d-%H-%M-%S-', time.localtime(time.time()))
        rect_render_thread = threading.Thread(
            target=self.rect_render_task,
            args=(current_idx, current_time, frame_cache,
                  rect_cache, render_cache,), daemon=True)
        # rect_render_thread.setDaemon(True)
        rect_render_thread.start()
        # self.rect_render_task(current_idx, current_time, frame_cache, rect_cache, render_cache)
        # self.original_render_task(current_idx, current_time, frame_cache)
        original_render_thread = threading.Thread(
            target=self.original_render_task,
            args=(current_idx, current_time, frame_cache,), daemon=True)
        # original_render_thread.setDaemon(True)
        original_render_thread.start()
        self.write_done = True
        self.stream_cnt += 1
        return True

    def rect_render_task(self, current_idx, current_time, frame_cache, rect_cache, render_cache):
        start = time.time()
        target = self.rect_stream_path / (current_time + str(self.stream_cnt) + '.mp4')
        logger.info(
            f'Video Render [{self.index}]: Rect Render Task [{self.stream_cnt}]: Writing detection stream frame into: [{str(target)}]')
        # fourcc = cv2.VideoWriter_fourcc(*'avc1')
        video_write = cv2.VideoWriter(str(target), self.fourcc, 24.0, (1920, 1080), True)
        next_cnt = current_idx - self.future_frames
        next_cnt = self.write_render_video_work(video_write, next_cnt, current_idx, render_cache, rect_cache,
                                                frame_cache)
        # the future frames count
        # next_frame_cnt = 48
        # wait the futures frames is accessable
        if not self.next_prepare_event.is_set():
            logger.info(
                f'Video Render [{self.index}]: Rect Render Task [{self.stream_cnt}] wait frames accessible....')
            start = time.time()
            # wait the future frames prepared,if ocurring time out, give up waits
            self.next_prepare_event.wait(30)
            logger.info(
                f"Video Render [{self.controller.cfg.index}]: Rect Render Task " +
                f"[{self.stream_cnt}] wait [{round(time.time() - start, 2)}] seconds")
            logger.info(f'Video Render [{self.index}]: Rect Render Task [{self.stream_cnt}] frames accessible...')

        # if not self.started:
        #     return False
        # logger.info('Render task Begin with frame [{}]'.format(next_cnt))
        # logger.info('After :[{}]'.format(render_cache.keys()))
        end_cnt = next_cnt + self.future_frames
        next_cnt = self.write_render_video_work(video_write, next_cnt, end_cnt, render_cache, rect_cache,
                                                frame_cache)
        video_write.release()
        logger.info(
            f'Video Render [{self.index}]: Rect Render Task [{self.stream_cnt}]: Consume [{time.time() - start}] ' +
            f'seconds.Done write detection stream frame into: [{str(target)}]')
        msg_json = creat_packaged_msg_json(filename=str(target.name), path=str(target))
        self.msg_queue.put(msg_json)
        logger.info(f'put packaged message in the msg_queue...')

    def original_render_task(self, current_idx, current_time, frame_cache):
        start = time.time()
        target = self.original_stream_path / (current_time + str(self.stream_cnt) + '.mp4')
        logger.info(
            f'Video Render [{self.index}]: Original Render Task [{self.stream_cnt}]: Writing detection stream frame into: [{str(target)}]')
        video_write = cv2.VideoWriter(str(target), self.fourcc, 24.0, (1920, 1080), True)
        next_cnt = current_idx - self.future_frames
        next_cnt = self.write_original_video_work(video_write, next_cnt, current_idx, frame_cache)
        # the future frames count
        # next_frame_cnt = 48
        # wait the futures frames is accessable
        if not self.next_prepare_event.is_set():
            logger.info(f'Video Render [{self.index}]: Original Render Task wait frames accessible....')
            start = time.time()
            # wait the future frames prepared,if ocurring time out, give up waits
            self.next_prepare_event.wait(30)
            logger.info(
                f"Video Render [{self.controller.cfg.index}]: Original Render Task [{self.stream_cnt}] wait [{time.time() - start}] seconds")
            logger.info(f'Video Render [{self.index}]: Original Render Task [{self.stream_cnt}] frames accessible....')
        # logger.info('Render task Begin with frame [{}]'.format(next_cnt))
        # logger.info('After :[{}]'.format(render_cache.keys()))
        # if not self.started:
        #     return False
        end_cnt = next_cnt + self.future_frames
        next_cnt = self.write_original_video_work(video_write, next_cnt, end_cnt, frame_cache)
        video_write.release()
        logger.info(
            f'Video Render [{self.index}]: Original Render Task [{self.stream_cnt}]: ' +
            f'Consume [{round(time.time() - start, 2)}] seconds.Done write detection stream frame into: [{str(target)}]')


class ProcessBasedDetectorController(DetectorController):

    def start(self, pool: Pool):
        super().start(pool)
        res = pool.apply_async(self.collect_and_reconstruct, (None, None))
        pool.apply_async(self.dispatch, ())
        pool.apply_async(self.write_frame_work)
        logger.info('Running detectors.......')
        detect_proc_res = []
        for idx, d in enumerate(self.detectors):
            logger.info('Submit detector [{},{},{}] task..'.format(self.cfg.index, d.x_index, d.y_index))
            detect_proc_res.append(pool.apply_async(d.detect, ()))
            # detect_proc_res.append(pool.submit(d.detect, ()))
            logger.info('Done detector [{},{},{}]'.format(self.cfg.index, d.x_index, d.y_index))
        return res, detect_proc_res
        # self.monitor.wait_pool()
        # self.loop_work()


class ThreadBasedDetectorController(DetectorController):

    def start(self, pool: ThreadPoolExecutor):
        super().start(pool)
        thread_res = []
        try:
            thread_res.append(pool.submit(self.collect_and_reconstruct))
            thread_res.append(pool.submit(self.dispatch))
            logger.info('Running detectors.......')
            for idx, d in enumerate(self.detectors):
                logger.info(
                    'Submit detector [{},{},{}] task..'.format(self.cfg.index, d.x_index, d.y_index))
                thread_res.append(pool.submit(d.detect))
                # detect_proc_res.append(pool.submit(d.detect, ()))
                logger.info('Done detector [{},{},{}]'.format(self.cfg.index, d.x_index, d.y_index))
        except Exception as e:
            traceback.print_exc()
            logger.error(e)
        return thread_res
        # self.monitor.wait_pool()
        # self.loop_work()


class TaskBasedDetectorController(ThreadBasedDetectorController):

    def __init__(self, cfg: VideoConfig, stream_path: Path, candidate_path: Path, frame_path: Path, frame_queue: Queue,
                 index_pool: Queue, msg_queue: Queue) -> None:
        super().__init__(cfg, stream_path, candidate_path, frame_path, frame_queue, index_pool, msg_queue)
        # self.construct_params = ray.put(
        #     ConstructParams(self.result_queue, self.original_frame_cache, self.render_frame_cache,
        #                     self.render_rect_cache, self.stream_render, 500, self.cfg))
        self.construct_params = ConstructParams(self.result_queue, self.original_frame_cache, self.render_frame_cache,
                                                self.render_rect_cache, self.stream_render, 500, self.cfg)
        # self.pool = ThreadPoolExecutor()
        # self.threads = []
        self.display_pipe = Manager().Queue(1000)
        self.detect_params = []
        self.detectors = []

    def init_detectors(self):
        logger.info(
            '*******************************Controller [{}]: Init total [{}] detectors********************************'.format(
                self.cfg.index,
                self.x_num * self.y_num))
        for i in range(self.x_num):
            for j in range(self.y_num):
                region_detector_path = self.block_path / (str(i) + '-' + str(j))
                # index = self.col * i + j
                # self.detectors.append(
                #     TaskBasedDetector(self.col_step, self.row_step, i, j, self.cfg, self.send_pipes[index],
                #                       self.receive_pipes[index],
                #                       region_detector_path))
                self.detect_params.append(
                    DetectorParams(self.x_step, self.y_step, i, j, self.cfg, region_detector_path))
        logger.info(
            '*******************************Controller [{}]: detectors init done ********************************'.format(
                self.cfg.index))

    def init_control_range(self):
        # read a frame, record frame size before running detectors
        empty = np.zeros(self.cfg.shape).astype(np.uint8)
        frame, _ = preprocess(empty, self.cfg)
        self.x_step = int(frame.shape[1] / self.x_num)
        self.y_step = int(frame.shape[0] / self.y_num)
        self.block_info = BlockInfo(self.y_num, self.x_num, self.y_step, self.x_step)

    def collect(self, args):
        return [f.result() for f in args]

    def collect_and_reconstruct(self, *args):
        collect_start = time.time()
        # results = self.collect(args)
        # results = args[0]
        logger.info('Controller [{}]: Collect consume [{}] seconds'.format(self.cfg.index, time.time() - collect_start))
        construct_result: ConstructResult = self.construct(*args)
        if construct_result is not None:
            frame = construct_result.frame
            if self.cfg.draw_boundary:
                frame, _ = preprocess(frame, self.cfg)
                frame = draw_boundary(frame, self.block_info)
                # logger.info('Done constructing of sub-frames into a original frame....')
            if self.cfg.show_window:
                self.pipe[0].send(construct_result)
            # if self.cfg.show_window:
            #     frame = imutils.resize(frame, width=800)
            #     self.display_pipe.put(frame)
            # cv2.imshow('Reconstructed Frame', frame)
            # cv2.waitKey(1)
        else:
            logger.error('Empty reconstruct result.')
        return True

    def dispatch_based_queue(self):
        # start = time.time()
        while True:
            if self.quit:
                break
            frame = self.frame_queue.get()
            self.dispatch_frame(frame)
        return True

    def send(self, frame):
        self.pipe[0].send(frame)

    def construct(self, *args):
        # sub_frames = [r.frame for r in results]
        results = args[0]
        _model = args[1]
        sub_binary = [r.binary for r in results]
        # sub_thresh = [r.thresh for r in results]
        # constructed_frame = self.construct_rgb(sub_frames)
        constructed_binary = self.construct_gray(sub_binary)
        # constructed_thresh = self.construct_gray(sub_thresh)
        logger.info(f'Controller [{self.cfg.index}]: Construct frames into a original frame....')
        try:
            self.construct_cnt += 1
            current_index = results[0].frame_index
            while current_index not in self.original_frame_cache:
                logger.info(
                    f'Current index: [{current_index}] not in original frame cache.May cache was cleared by timer')
                time.sleep(0.5)
                # logger.info(self.original_frame_cache.keys())
            original_frame = self.original_frame_cache[current_index]
            render_frame = original_frame.copy()
            for r in results:
                if len(r.rects):
                    self.result_queue.put((original_frame, r.frame_index, r.rects))
                    if r.frame_index not in self.original_frame_cache:
                        logger.info('Unknown frame index: [{}] to fetch frame in cache.'.format(r.frame_index))
                        continue
                    for rect in r.rects:
                        candidate = crop_by_rect(self.cfg, rect, render_frame)
                        if _model.predict(candidate) == 0:
                            diff_frame = current_index - self.last_detection
                            if self.last_detection != -1 and 0 < diff_frame < self.cfg.detect_internal:
                                hit_precision = 0
                                hit_cnt = 0
                                for idx in range(current_index + 1, current_index + 24):
                                    if idx in self.original_frame_cache:
                                        history_frame = self.original_frame_cache[idx]
                                        sub_results = self.post_detect(history_frame, idx)
                                        for sr_idx, sr in enumerate(sub_results):
                                            rl = min(len(r.rects), len(sr.rects))
                                            for rl_idx in range(rl):
                                                sr_patch = crop_by_rect(self.cfg, sr.rects[rl_idx], sr.binary)
                                                r_patch = crop_by_rect(self.cfg, results[sr_idx].rects[rl_idx],
                                                                       r.binary)
                                                white_num = np.sum((sr_patch == r_patch).astype(np.int))
                                                logger.info(
                                                    f'Controller [{self.cfg.index}]: White pixed num {white_num}')
                                                per_hit_precision = white_num / np.sum(r_patch)
                                                logger.info(
                                                    f'Controller [{self.cfg.index}]: Per Hit precision {round(per_hit_precision, 2)}')
                                                hit_precision += per_hit_precision
                                                hit_cnt += 1
                                hit_precision /= hit_cnt
                                logger.info(
                                    f'Controller [{self.cfg.index}]: Final Hit precision {round(hit_precision, 2)}')
                                # logger.info(
                                #     f'Controller [{self.cfg.index}]: Continuous detection report after' +
                                #     f'[{round(detect_internal, 2)} seconds].Skipped.............')
                                return ConstructResult(original_frame, constructed_binary, None)
                            logger.info(
                                f'============================Controller [{self.cfg.index}]: Dolphin Detected============================')
                            json_msg = creat_detect_msg_json(video_stream=self.cfg.rtsp, channel=self.cfg.index,
                                                             timestamp=current_index, rects=r.rects)
                            self.msg_queue.put(json_msg)
                            logger.info(f'put detect message in msg_queue...')

                            if self.cfg.render:
                                color = np.random.randint(0, 255, size=(3,))
                                color = [int(c) for c in color]
                                p1, p2 = bbox_points(self.cfg, rect, render_frame.shape)
                                cv2.rectangle(render_frame, p1, p2, color, 2)
                                self.render_frame_cache[current_index] = render_frame
                                self.render_rect_cache[current_index] = r.rects
                                threading.Thread(target=self.stream_render.reset, args=(current_index,),
                                                 daemon=True).start()
                            self.last_detection = self.stream_render.next_st(current_index)
                            # Process(target=self.stream_render.reset, daemon=False, args=(current_index,)).start()
                    # self.stream_render.reset(current_index)
            # self.stream_render.notify(current_index)
            if self.cfg.render:
                # Process(target=self.stream_render.notify, daemon=False, args=(current_index,)).start()
                threading.Thread(target=self.stream_render.notify, args=(current_index,), daemon=True).start()

            self.clear_render_cache()
            # return constructed_frame, constructed_binary, constructed_thresh
            return ConstructResult(original_frame, constructed_binary, None)
        except Exception as e:
            traceback.print_exc()
            logger.error(e)

    def post_detect(self, frame, idx) -> List[DetectionResult]:
        sub_results = []
        for d in self.detect_params:
            block = DispatchBlock(crop_by_se(frame, d.start, d.end),
                                  idx, frame.shape)
            sub_results.append(detect_based_task(block, d))
        return sub_results

    def construct(self, *args):
        # sub_frames = [r.frame for r in results]
        results = args[0]
        _model = args[1]
        sub_binary = [r.binary for r in results]
        # sub_thresh = [r.thresh for r in results]
        # constructed_frame = self.construct_rgb(sub_frames)
        constructed_binary = self.construct_gray(sub_binary)
        # constructed_thresh = self.construct_gray(sub_thresh)
        logger.info(f'Controller [{self.cfg.index}]: Construct frames into a original frame....')
        try:
            self.construct_cnt += 1
            current_index = results[0].frame_index
            while current_index not in self.original_frame_cache:
                logger.info(
                    f'Current index: [{current_index}] not in original frame cache.May cache was cleared by timer')
                time.sleep(0.5)
                # logger.info(self.original_frame_cache.keys())
            original_frame = self.original_frame_cache[current_index]
            render_frame = original_frame.copy()
            for r in results:
                if len(r.rects):
                    self.result_queue.put((original_frame, r.frame_index, r.rects))
                    if r.frame_index not in self.original_frame_cache:
                        logger.info('Unknown frame index: [{}] to fetch frame in cache.'.format(r.frame_index))
                        continue
                    for rect in r.rects:
                        candidate = crop_by_rect(self.cfg, rect, render_frame)
                        if _model.predict(candidate) == 0:
                            is_filtered = self.filter_continuous_detect(current_index, original_frame, len(r.rects),
                                                                        results)
                            if is_filtered:
                                # self.last_detection = current_index
                                return ConstructResult(original_frame, constructed_binary, None)
                            logger.info(
                                f'============================Controller [{self.cfg.index}]: Dolphin Detected============================')
                            json_msg = creat_detect_msg_json(video_stream=self.cfg.rtsp, channel=self.cfg.index,
                                                             timestamp=current_index, rects=r.rects)
                            self.msg_queue.put(json_msg)
                            logger.info(f'put detect message in msg_queue...')
                            if self.cfg.render:
                                color = np.random.randint(0, 255, size=(3,))
                                color = [int(c) for c in color]
                                p1, p2 = bbox_points(self.cfg, rect, render_frame.shape)
                                cv2.rectangle(render_frame, p1, p2, color, 2)
                                self.render_frame_cache[current_index] = render_frame
                                self.render_rect_cache[current_index] = r.rects
                                threading.Thread(target=self.stream_render.reset, args=(current_index,),
                                                 daemon=True).start()
                            self.last_detection = self.stream_render.next_st(current_index)
                            # Process(target=self.stream_render.reset, daemon=False, args=(current_index,)).start()
                    # self.stream_render.reset(current_index)
            # self.stream_render.notify(current_index)
            if self.cfg.render:
                # Process(target=self.stream_render.notify, daemon=False, args=(current_index,)).start()
                threading.Thread(target=self.stream_render.notify, args=(current_index,), daemon=True).start()

            self.clear_render_cache()
            # return constructed_frame, constructed_binary, constructed_thresh
            return ConstructResult(original_frame, constructed_binary, None)
        except Exception as e:
            traceback.print_exc()
            logger.error(e)

    def filter_continuous_detect(self, current_index, original_frame, len_rect, results):
        diff_frame = current_index - self.last_detection
        if self.continuous_filter_flag:
            logger.info(f'Controller [{self.cfg.index}]: Frame [{current_index}] is still in filter window range')
            return True
        if diff_frame > self.cfg.detect_internal:
            self.continuous_filter_flag = False
        if self.last_detection != -1 and 0 < diff_frame <= self.cfg.detect_internal:
            logger.info(
                f'****************************Controller [{self.cfg.index}]: '
                f'Enter continuous exception handle process at Frame '
                f'[{current_index}]***********************************')

            logger.info(f'Controller [{self.cfg.index}]: Diff frame [{diff_frame}]')
            hit_precision = 0
            hit_cnt = 0
            start = time.time()
            for idx in range(current_index + 1, current_index + self.cfg.search_window_size):
                if idx in self.original_frame_cache:
                    history_frame = self.original_frame_cache[idx]
                    sub_results = self.post_detect(history_frame, idx)
                    for sr_idx, sr in enumerate(sub_results):
                        rl = min(len_rect, len(sr.rects))
                        for rl_idx in range(rl):
                            sr_rgb_patch = crop_by_rect(self.cfg, sr.rects[rl_idx], history_frame)
                            r_rgb_patch = crop_by_rect(self.cfg, results[sr_idx].rects[rl_idx],
                                                       original_frame)
                            cosine_similarity = cal_hist_cosin_similarity(sr_rgb_patch, r_rgb_patch)
                            logger.info(
                                f'Controller [{self.cfg.index}]: Cosine Distance {round(cosine_similarity, 2)}')
                            logger.info(
                                f'Controller [{self.cfg.index}]: Frame [{idx}]: cosine similarity '
                                f'{round(cosine_similarity, 2)}')
                            hit_precision += cosine_similarity
                            # cv2.imwrite('data/test/0208/' + str(current_index) + '_r_' + str(
                            #     hit_cnt) + '.png', r_patch)
                            # cv2.imwrite('data/test/0208/' + str(current_index) + '_sr_' + str(
                            #     hit_cnt) + '.png', sr_patch)
                            hit_cnt += 1
            if hit_cnt:
                hit_precision /= hit_cnt
                logger.info(
                    f'Controller [{self.cfg.index}]: Hit count{hit_cnt}')
            logger.info(
                f'Controller [{self.cfg.index}]: Average hit precision {round(hit_precision, 2)}')
            logger.info(
                self.LOG_PREFIX + f'continuous exception handle process consumes [{round(time.time() - start, 2)}]')
            if hit_cnt and hit_precision >= 0.6:
                logger.info(
                    f'Controller [{self.cfg.index}]: Continuous detection report at' +
                    f'Frame [{current_index}].Skipped by continuous exception filter rule.............')
                self.continuous_filter_flag = True
                return True
            return False

    def dispatch_frame(self, *args):
        start = time.time()
        frame = args[0]
        self.frame_cnt.set(self.frame_cnt.get() + 1)
        self.original_frame_cache[self.frame_cnt.get()] = frame
        # threading.Thread(target=self.send, args=(frame,)).start()
        # self.render_frame_cache[self.frame_cnt.get()] = frame
        # logger.info(self.original_frame_cache.keys())
        if self.frame_cnt.get() <= self.cfg.pre_cache:
            return

        if self.frame_cnt.get() % self.cfg.sample_rate == 0:
            logger.info('Controller [{}]: Dispatch frame to all detectors....'.format(self.cfg.index))
            async_futures = []
            pre_index = self.frame_cnt.get() - self.cfg.pre_cache
            original_frame = self.original_frame_cache[pre_index]
            frame, original_frame = preprocess(original_frame, self.cfg)
            for d in self.detect_params:
                block = DispatchBlock(crop_by_se(frame, d.start, d.end),
                                      pre_index, original_frame.shape)
                # async_futures.append(pool.apply_async(d.detect_based_task, (block,)))
                async_futures.append(detect_based_task(block, d))
                # detect_td = threading.Thread(
                #     target=detect_based_task,
                #     args=(detect_based_task, block, d))
                # async_futures.append(detect_td.start())
                # async_futures.append(self.pool.submit(detect_based_task, block, d))
                # async_futures.append(detect_based_task.remote(block, d))
            self.collect_and_reconstruct(async_futures, args[1])
            # r = pool.apply_async(collect_and_reconstruct,
            #                      (async_futures, self.construct_params, self.block_info, self.cfg,))
            # r.get()
            # collect_and_reconstruct.remote(async_futures, self.construct_params, self.block_info, self.cfg)
        # self.dispatch_cnt += 1
        # if self.dispatch_cnt % 100 == 0:
        #     end = time.time() - start
        #     logger.info(
        #         'Detection controller [{}]: Operation Speed Rate [{}]s/100fs, unit process rate: [{}]s/f'.format(
        #             self.cfg.index, round(end, 2), round(end / 100, 2)))
        #     self.dispatch_cnt = 0
        self.clear_original_cache()

    def display(self):
        logger.info(
            '*******************************Controller [{}]: Init video player********************************'.format(
                self.cfg.index))
        while True:
            if self.status.get() == SystemStatus.SHUT_DOWN:
                logger.info(
                    '*******************************Controller [{}]: Video player exit********************************'.format(
                        self.cfg.index))
                break
            try:
                if not self.display_pipe.empty():
                    frame = self.display_pipe.get(timeout=1)
                    cv2.imshow('Controller {}'.format(self.cfg.index), frame)
                    cv2.waitKey(1)
            except Exception as e:
                logger.error(e)
        return True

    # def call_task(self, frame):
    #     self.dispatch_frame(frame)

    def start(self, pool: Pool):
        self.status.set(SystemStatus.RUNNING)
        self.init_control_range()
        self.init_detectors()
        # self.dispatch_based_queue(pool)
        # res = self.pool.submit(self.write_frame_work, ())
        threading.Thread(target=self.listen, daemon=True).start()
        threading.Thread(target=self.write_frame_work, daemon=True).start()
        # threading.Thread(target=self.display, daemon=True).start()
        return True


class ProcessAndThreadBasedDetectorController(DetectorController):

    def start(self, pool):
        process_pool = pool[0]
        thread_pool = pool[1]
        pool_res = []
        thread_res = []
        super().start(process_pool)
        # collect child frames and reconstruct frames from detectors asynchronously
        pr1 = process_pool.apply_async(self.collect_and_reconstruct, ())
        pool_res.append(pr1)
        # dispatch child frames to detector asynchronously
        pr2 = process_pool.apply_async(self.dispatch, ())
        # write detection result asynchronously
        thread_res.append(thread_pool.submit(self.write_frame_work))
        pool_res.append(pr2)
        logger.info('Running detectors.......')
        for idx, d in enumerate(self.detectors):
            logger.info('Submit detector [{},{},{}] task..'.format(self.cfg.index, d.x_index, d.y_index))
            thread_res.append(thread_pool.submit(d.detect))
            # detect_proc_res.append(pool.submit(d.detect, ()))
            logger.info('Done detector [{},{},{}]'.format(self.cfg.index, d.x_index, d.y_index))
        return pool_res, thread_res
        # self.monitor.wait_pool()
        # self.loop_work()

    def dispatch_frame(self, *args):
        start = time.time()
        frame = args[0]
        self.frame_cnt.set(self.frame_cnt.get() + 1)
        self.original_frame_cache[self.frame_cnt.get()] = frame
        # threading.Thread(target=self.send, args=(frame,)).start()
        # self.render_frame_cache[self.frame_cnt.get()] = frame
        # logger.info(self.original_frame_cache.keys())
        if self.frame_cnt.get() <= self.cfg.pre_cache:
            return

        pre_index = self.frame_cnt.get() - self.cfg.pre_cache
        if pre_index % self.cfg.sample_rate == 0:
            logger.info('Controller [{}]: Dispatch frame to all detectors....'.format(self.cfg.index))
            async_futures = []
            original_frame = self.original_frame_cache[pre_index]
            frame, original_frame = preprocess(original_frame, self.cfg)
            for d in self.detect_params:
                block = DispatchBlock(crop_by_se(frame, d.start, d.end),
                                      pre_index, original_frame.shape)
                # async_futures.append(pool.apply_async(d.detect_based_task, (block,)))
                async_futures.append(detect_based_task(block, d))
                # detect_td = threading.Thread(
                #     target=detect_based_task,
                #     args=(detect_based_task, block, d))
                # async_futures.append(detect_td.start())
                # async_futures.append(self.pool.submit(detect_based_task, block, d))
                # async_futures.append(detect_based_task.remote(block, d))
            self.collect_and_reconstruct(async_futures, args[1])
            # r = pool.apply_async(collect_and_reconstruct,
            #                      (async_futures, self.construct_params, self.block_info, self.cfg,))
            # r.get()
            # collect_and_reconstruct.remote(async_futures, self.construct_params, self.block_info, self.cfg)
        # self.dispatch_cnt += 1
        # if self.dispatch_cnt % 100 == 0:
        #     end = time.time() - start
        #     logger.info(
        #         'Detection controller [{}]: Operation Speed Rate [{}]s/100fs, unit process rate: [{}]s/f'.format(
        #             self.cfg.index, round(end, 2), round(end / 100, 2)))
        #     self.dispatch_cnt = 0
        self.clear_original_cache()

    def display(self):
        logger.info(
            '*******************************Controller [{}]: Init video player********************************'.format(
                self.cfg.index))
        while True:
            if self.status.get() == SystemStatus.SHUT_DOWN:
                logger.info(
                    '*******************************Controller [{}]: Video player exit********************************'.format(
                        self.cfg.index))
                break
            try:
                if not self.display_pipe.empty():
                    frame = self.display_pipe.get(timeout=1)
                    cv2.imshow('Controller {}'.format(self.cfg.index), frame)
                    cv2.waitKey(1)
            except Exception as e:
                logger.error(e)
        return True

    # def call_task(self, frame):
    #     self.dispatch_frame(frame)

    def start(self, pool: Pool):
        self.status.set(SystemStatus.RUNNING)
        self.init_control_range()
        self.init_detectors()
        # self.dispatch_based_queue(pool)
        # res = self.pool.submit(self.write_frame_work, ())
        threading.Thread(target=self.listen, daemon=True).start()
        threading.Thread(target=self.write_frame_work, daemon=True).start()
        # threading.Thread(target=self.display, daemon=True).start()
        return True


class ProcessAndThreadBasedDetectorController(DetectorController):

    def start(self, pool):
        process_pool = pool[0]
        thread_pool = pool[1]
        pool_res = []
        thread_res = []
        super().start(process_pool)
        # collect child frames and reconstruct frames from detectors asynchronously
        pr1 = process_pool.apply_async(self.collect_and_reconstruct, ())
        pool_res.append(pr1)
        # dispatch child frames to detector asynchronously
        pr2 = process_pool.apply_async(self.dispatch, ())
        # write detection result asynchronously
        thread_res.append(thread_pool.submit(self.write_frame_work))
        pool_res.append(pr2)
        logger.info('Running detectors.......')
        for idx, d in enumerate(self.detectors):
            logger.info('Submit detector [{},{},{}] task..'.format(self.cfg.index, d.x_index, d.y_index))
            thread_res.append(thread_pool.submit(d.detect))
            # detect_proc_res.append(pool.submit(d.detect, ()))
            logger.info('Done detector [{},{},{}]'.format(self.cfg.index, d.x_index, d.y_index))
        return pool_res, thread_res
        # self.monitor.wait_pool()
        # self.loop_work()
