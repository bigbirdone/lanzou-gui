#!/usr/bin/env python3

import os
import re
from time import sleep
from PyQt5.QtCore import QThread, pyqtSignal, QMutex
import requests

from lanzou.api import LanZouCloud
from lanzou.api.utils import is_folder_url, is_file_url, logger
from lanzou.api.types import RecFolder, RecFile

from tools import DlJob, FileInfos, FolderInfos


def show_progress(file_name, total_size, now_size, symbol="█"):
    """显示进度条的回调函数"""
    percent = now_size / total_size
    # 进度条长总度
    file_len = len(file_name)
    if file_len >= 20:
        bar_len = 20
    elif file_len >= 10:
        bar_len = 30
    else:
        bar_len = 40
    if total_size >= 1048576:
        unit = "MB"
        piece = 1048576
    else:
        unit = "KB"
        piece = 1024
    bar_str = ("<font color='#00CC00'>" + symbol * round(bar_len * percent) +
               "</font><font color='#000080'>" + symbol * round(bar_len * (1 - percent)) + "</font>")
    msg = "\r{:>5.1f}%\t[{}] {:.1f}/{:.1f}{} | {} ".format(
        percent * 100,
        bar_str,
        now_size / piece,
        total_size / piece,
        unit,
        file_name,
    )
    if total_size == now_size:
        msg = msg + "| <font color='blue'>Done!</font>"
    return msg

def why_error(code):
    """错误原因"""
    if code == LanZouCloud.URL_INVALID:
        return '分享链接无效'
    elif code == LanZouCloud.LACK_PASSWORD:
        return '缺少提取码'
    elif code == LanZouCloud.PASSWORD_ERROR:
        return '提取码错误'
    elif code == LanZouCloud.FILE_CANCELLED:
        return '分享链接已失效'
    elif code == LanZouCloud.ZIP_ERROR:
        return '解压过程异常'
    elif code == LanZouCloud.NETWORK_ERROR:
        return '网络连接异常'
    else:
        return '未知错误'

def show_down_failed(code, file):
    """文件下载失败时的回调函数"""
    return f"文件下载失败,原因: {why_error(code)},文件名: {file.name},URL: {file.url}"


class Downloader(QThread):
    '''单个文件下载线程'''
    download_proc = pyqtSignal(str)
    download_failed = pyqtSignal(object, object)
    folder_file_failed = pyqtSignal(object, object)
    download_rate = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super(Downloader, self).__init__(parent)
        self._disk = None
        self._stopped = True
        self._mutex = QMutex()
        self.name = ""
        self.url = ""
        self.pwd = ""
        self.save_path = ""

    def set_disk(self, disk):
        self._disk = disk

    def stop(self):
        self._mutex.lock()
        self._stopped = True
        self._mutex.unlock()
        self.terminate()

    def _show_progress(self, file_name, total_size, now_size):
        """显示进度条的回调函数"""
        msg = show_progress(file_name, total_size, now_size)
        self.download_rate.emit(self.url, int(1000 * now_size/total_size))
        self.download_proc.emit(msg)

    def _down_failed(self, code, file):
        """显示下载失败的回调函数"""
        self.folder_file_failed.emit(code, file)

    # def __del__(self):
    #     self.wait()

    def set_values(self, name, url, pwd, save_path):
        self.name = name
        self.url = url
        self.pwd = pwd
        self.save_path = save_path
        self.start()

    def run(self):
        try:
            if is_file_url(self.url):  # 下载文件
                res = self._disk.down_file_by_url(self.url, self.pwd, self.save_path, self._show_progress)
            elif is_folder_url(self.url):  # 下载文件夹
                res = self._disk.down_dir_by_url(self.url, self.pwd, self.save_path, self._show_progress,
                                                 mkdir=True, failed_callback=self._down_failed)
            else:
                return
            if res == 0:
                self.download_rate.emit(self.url, 1000)
            else:
                self.download_failed.emit(self.url, res)
            logger.debug(f"Download res: {res}")
        except TimeoutError:
            logger.error("Download TimeOut")
            self.download_failed.emit(self.url, "网络连接错误！")
        except Exception as e:
            logger.error(f"Download error: {e=}")
            self.download_failed.emit(self.url, f"未知错误！{e}")


class DownloadManager(QThread):
    '''下载控制器线程，追加下载任务，控制后台下载线程数量'''
    downloaders_msg = pyqtSignal(str, int)
    update = pyqtSignal(dict)

    def __init__(self, threads=3, parent=None):
        super(DownloadManager, self).__init__(parent)
        self._disk = None
        self._tasks = {}
        self._thread = threads
        self._count = 0
        self._mutex = QMutex()
        self._is_work = False
        self._old_msg = ""
        self._dl_ing = {}
        self.downloaders = {}

    def set_disk(self, disk):
        self._disk = disk

    def set_thread(self, thread):
        self._thread = thread

    def stop_task(self, task):
        if self.downloaders[task.url].isRunning():
            self.downloaders[task.url].stop()
            self._dl_ing[task.url] = self._dl_ing[task.url]._replace(run=False)
            logger.debug(f"Stop job: {task}")
            self.update.emit(self._dl_ing)

    def start_task(self, task):
        if task.url not in self.downloaders:
            self.add_task(task)
        elif not self.downloaders[task.url].isRunning():
            logger.debug(f"Start job: {task}")
            self.downloaders[task.url].start()
            self._dl_ing[task.url] = self._dl_ing[task.url]._replace(run=True)
            self.update.emit(self._dl_ing)

    def add_task(self, task):
        if task.url not in self._tasks.keys():
            logger.debug(f"DownloadMgr add one: {task=}")
            self._tasks[task.url] = task
        self.start()

    def add_tasks(self, tasks: dict):
        logger.debug(f"DownloadMgr add: {tasks=}")
        self._tasks.update(tasks)
        self.start()

    def __del__(self):
        self.wait()

    def del_task(self, url):
        logger.debug(f"DownloadMgr del: {url=}")
        if url in self._dl_ing:
            del self._dl_ing[url]
        if url in self.downloaders:
            del self.downloaders[url]

    def _ahead_msg(self, msg):
        if self._old_msg != msg:
            if self._count == 1:
                self.downloaders_msg.emit(msg, 0)
            else:
                self.downloaders_msg.emit(f"有{self._count}个下载任务正在运行", 0)
            self._old_msg = msg

    def _ahead_error(self, url, error):
        self._dl_ing[url] = self._dl_ing[url]._replace(info=error)
        self.update.emit(self._dl_ing)

    def _ahead_folder_error(self, code, file):
        # 需要单独考虑，不在 task中
        pass
        # self._dl_ing[file.url] = code
        # self._dl_ing.emit(self._dl_ing)

    def _ahead_rate(self, url, rate):
        self._dl_ing[url] = self._dl_ing[url]._replace(rate=rate)
        self.update.emit(self._dl_ing)

    def _add_thread(self, url):
        logger.debug(f"DownloadMgr count: {self._count}")
        self._count -= 1
        del self.downloaders[url]

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            while True:
                if not self._tasks:
                    break
                while self._count >= self._thread:
                    self.sleep(1)
                self._count += 1
                url = list(self._tasks.keys())[0]
                task = self._tasks[url]
                logger.debug(f"DownloadMgr run: {task=}")
                self.downloaders[url] = Downloader()
                self.downloaders[url].set_disk(self._disk)
                self.downloaders_msg.emit("准备下载：<font color='#FFA500'>{}</font>".format(task.name), 8000)
                try:
                    self.downloaders[url].finished.connect(lambda: self._add_thread(url))
                    self.downloaders[url].download_proc.connect(self._ahead_msg)
                    self.downloaders[url].download_rate.connect(self._ahead_rate)
                    self.downloaders[url].folder_file_failed.connect(self._ahead_folder_error)
                    self.downloaders[url].download_failed.connect(self._ahead_error)
                    self._dl_ing[url] = task._replace(run=True)
                    self.downloaders[url].set_values(task.name, task.url, task.pwd, task.path)
                except Exception as exp:
                    logger.error(f"DownloadMgr Error: {exp=}")
                del self._tasks[url]
            self._is_work = False
            self._mutex.unlock()


class GetSharedInfo(QThread):
    '''提取界面获取分享链接信息'''
    infos = pyqtSignal(object)
    msg = pyqtSignal(str, int)
    update = pyqtSignal()

    def __init__(self, parent=None):
        super(GetSharedInfo, self).__init__(parent)
        self._disk = None
        self.share_url = ""
        self.pwd = ""
        self.is_file = ""
        self.is_folder = ""
        self._mutex = QMutex()
        self._is_work = False
        self._pat = r"(https?://(\w[-\w]*\.)?lanzous.com/[bi]?[A-Za-z0-9]+)[^0-9a-z]*([a-z0-9]+)?"

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, text):
        '''获取分享链接信息'''
        if not text:
            return
        for share_url, _, pwd in re.findall(self._pat, text):
            if is_file_url(share_url):  # 文件链接
                is_file = True
                is_folder = False
                self.msg.emit("正在获取文件链接信息……", 20000)
            elif is_folder_url(share_url):  # 文件夹链接
                is_folder = True
                is_file = False
                self.msg.emit("正在获取文件夹链接信息，可能需要几秒钟，请稍后……", 30000)
            else:
                self.msg.emit(f"{share_url} 为非法链接！", 0)
                return
            self.update.emit()  # 清理旧的显示信息
            self.share_url = share_url
            self.pwd = pwd
            self.is_file = is_file
            self.is_folder = is_folder
            self.start()
            break

    def __del__(self):
        self.wait()

    def stop(self):  # 用于手动停止
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def emit_msg(self, infos):
        '''根据查询信息发送状态信号'''
        show_time = 2999  # 提示显示时间，单位 ms
        if infos.code == LanZouCloud.FILE_CANCELLED:
            self.msg.emit("<font color='red'>文件不存在，或已删除！</font>", show_time)
        elif infos.code == LanZouCloud.URL_INVALID:
            self.msg.emit("<font color='red'>链接非法！</font>", show_time)
        elif infos.code == LanZouCloud.PASSWORD_ERROR:
            self.msg.emit("<font color='red'>提取码 [<b><font color='magenta'>{}</font></b>] 错误！</font>".format(self.pwd), show_time)
        elif infos.code == LanZouCloud.LACK_PASSWORD:
            self.msg.emit("<font color='red'>请在链接后面跟上提取码，空格分割！</font>", show_time)
        elif infos.code == LanZouCloud.NETWORK_ERROR:
            self.msg.emit("<font color='red'>网络错误！{}</font>".format(infos["info"]), show_time)
        elif infos.code == LanZouCloud.SUCCESS:
            self.msg.emit("<font color='#00CC00'>提取成功！</font>", show_time)

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            try:
                if self.is_file:  # 链接为文件
                    _infos = self._disk.get_share_info_by_url(self.share_url, self.pwd)
                    self.emit_msg(_infos)
                elif self.is_folder:  # 链接为文件夹
                    _infos = self._disk.get_folder_info_by_url(self.share_url, self.pwd)
                    self.emit_msg(_infos)
                self.infos.emit(_infos)
            except TimeoutError:
                self.msg.emit("font color='red'>网络超时！请稍后重试</font>", 5000)
            except Exception as e:
                logger.error(f"GetShareInfo error: {e=}")
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("<font color='blue'>后台正在运行，稍后重试！</font>", 4000)


class UploadWorker(QThread):
    '''文件上传线程'''
    code = pyqtSignal(str, int)
    update = pyqtSignal(dict)

    def __init__(self, parent=None):
        super(UploadWorker, self).__init__(parent)
        self._disk = None
        self._tasks = {}
        self._mutex = QMutex()
        self._is_work = False
        self._furl = ""
        self._task = None

    def _show_progress(self, file_name, total_size, now_size):
        """显示进度条的回调函数"""
        msg = show_progress(file_name, total_size, now_size, symbol="█")
        rate = int(1000 * now_size/total_size)
        self._task = self._task._replace(rate=rate)
        self.update.emit({self._furl: self._task})
        self.code.emit(msg, 0)

    def set_disk(self, disk):
        self._disk = disk

    def add_task(self, task):
        if task.furl not in self._tasks.keys():
            logger.debug(f"upload add one task: {task}")
            self._tasks[task.furl] = task
        self.start()

    def add_tasks(self, tasks: dict):
        logger.debug(f"upload add tasks: {tasks.values()}")
        self._tasks.update(tasks)
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):  # 用于手动停止
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            while True:
                if not self._tasks:
                    logger.debug(f"upload finished!")
                    break
                self._furl = list(self._tasks.keys())[0]
                self._task = self._tasks[self._furl]
                logger.debug(f"run task: {self._task=}")
                if not os.path.exists(self._furl):
                    logger.error(f"upload file not exist : {self._furl}")
                    msg = f"<b>ERROR :</b> <font color='red'>文件不存在:{self._furl}</font>"
                    self.code.emit(msg, 3100)
                    continue
                if os.path.isdir(self._furl):
                    logger.error(f"upload dir : {self._furl}")
                    msg = f"<b>INFO :</b> <font color='#00CC00'>批量上传文件夹:{self._furl}</font>"
                    self.code.emit(msg, 30000)
                    self._disk.upload_dir(self._furl, self._task.id, self._show_progress, None)
                else:
                    msg = f"<b>INFO :</b> <font color='#00CC00'>上传文件:{self._furl}</font>"
                    self.code.emit(msg, 20000)
                    try:
                        code, fid, isfile = self._disk.upload_file(self._task.furl, self._task.id, self._show_progress)
                    except TimeoutError:
                        msg = "<b>ERROR :</b> <font color='red'>网络连接超时，请重试！</font>"
                        self.code.emit(msg, 3100)
                        self._task = self._task._replace(info="网络连接超时")
                        self.update.emit({self._furl: self._task})
                    except Exception as e:
                        logger.error(f"UploadWorker error: {e=}")
                        self._task = self._task._replace(info="未知错误")
                        self.update.emit({self._furl: self._task})
                    else:
                        self._task = self._task._replace(info="上传成功")
                        if code == LanZouCloud.SUCCESS:
                            if self._task.set_pwd:
                                self._disk.set_passwd(fid, self._task.pwd, is_file=isfile)
                            if self._task.set_desc:
                                self._disk.set_desc(fid, self._task.desc, is_file=isfile)
                del self._tasks[self._furl]
            self._is_work = False
            self._mutex.unlock()


class LoginLuncher(QThread):
    '''登录线程'''
    code = pyqtSignal(bool, str, int)
    update_cookie = pyqtSignal(object, str)

    def __init__(self, parent=None):
        super(LoginLuncher, self).__init__(parent)
        self._disk = None
        self.username = ""
        self.password = ""
        self.cookie = None

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, username, password, cookie=None):
        self.username = username
        self.password = password
        self.cookie = cookie
        self.start()

    def __del__(self):
        self.wait()

    def run(self):
        try:
            if self.cookie:
                res = self._disk.login_by_cookie(self.cookie)
                if res == LanZouCloud.SUCCESS:
                    self.code.emit(True, "<font color='#00CC00'>通过<b>Cookie</b>登录<b>成功</b>！ ≧◉◡◉≦</font>", 5000)
                    return
                logger.debug(f"login by Cookie err: {res=}")
            if (not self.username or not self.password) and not self.cookie:
                logger.debug("login err: No UserName、No cookie")
                self.code.emit(False, "<font color='red'>登录失败: 没有用户或密码</font>", 3000)
            else:
                res = self._disk.login(self.username, self.password)
                if res == LanZouCloud.SUCCESS:
                    self.code.emit(True, "<font color='#00CC00'>登录<b>成功</b>！ ≧◉◡◉≦</font>", 5000)
                    _cookie = self._disk.get_cookie()
                    self.update_cookie.emit(_cookie, str(self.username))
                else:
                    logger.debug(f"login err: {res=}")
                    self.code.emit(False, "<font color='red'>登录失败，可能是用户名或密码错误！</font>", 8000)
                    self.update_cookie.emit(None, str(self.username))
        except TimeoutError:
            self.code.emit(False, "<font color='red'>网络超时！</font>", 3000)
        except Exception as e:
                logger.error(f"LoginLuncher error: {e=}")


class DescPwdFetcher(QThread):
    '''获取描述与提取码 线程'''
    desc = pyqtSignal(object)
    tasks = pyqtSignal(object)
    msg = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super(DescPwdFetcher, self).__init__(parent)
        self._disk = None
        self.infos = None
        self.download = False
        self.dl_path = ""
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, infos, download=False, dl_path=""):
        self.infos = infos  # 列表的列表
        self.download = download  # 标识激发下载器
        self.dl_path = dl_path
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            try:
                if not self.infos:
                    raise UserWarning
                _tasks = {}
                _infos = []
                for info in self.infos:
                    if info.id:  # disk 运行
                        if info.is_file:  # 文件
                            res = self._disk.get_share_info(info.id, is_file=True)
                        else:  # 文件夹
                            res = self._disk.get_share_info(info.id, is_file=False)
                        if res.code == LanZouCloud.SUCCESS:
                            info.pwd = res.pwd
                            info.url = res.url
                            info.desc = res.desc
                        elif res.code == LanZouCloud.NETWORK_ERROR:
                            self.msg.emit("网络错误，请稍后重试！", 6000)
                            continue
                    _infos.append(info)
                    _tasks[info.url] = DlJob(name=info.name, url=info.url, pwd=info.pwd, path=self.dl_path)
                if self.download:
                    self.tasks.emit(_tasks)
                else:  # 激发简介更新
                    self.desc.emit(_infos)
            except TimeoutError:
                self.msg.emit("网络超时，请稍后重试！", 6000)
            except UserWarning:
                pass
            except Exception as e:
                logger.error(f"GetPwdFetcher error: {e=}")
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行指令！请稍后重试", 3100)


class ListRefresher(QThread):
    '''跟新目录文件与文件夹列表线程'''
    infos = pyqtSignal(object)
    err_msg = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super(ListRefresher, self).__init__(parent)
        self._disk = None
        self._fid = -1
        self.r_files = True
        self.r_folders = True
        self.r_path = True
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, fid, r_files=True, r_folders=True, r_path=True):
        if not self._is_work:
            self._fid = fid
            self.r_files = r_files
            self.r_folders = r_folders
            self.r_path = r_path
            self.start()
        else:
            self.err_msg.emit("正在更新目录，请稍后再试！", 3100)

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            emit_infos = {}
            # 传递更新内容
            emit_infos['r'] = {'fid': self._fid, 'files': self.r_files, 'folders': self.r_folders, 'path': self.r_path}
            try:
                if self.r_files:
                    # [i.id, i.name, i.size, i.time, i.downs, i.has_pwd, i.has_des]
                    info = {i.name: i for i in self._disk.get_file_list(self._fid)}
                    emit_infos['file_list'] = {key: info.get(key) for key in sorted(info.keys())}  # {name-File}
                if self.r_folders:
                    folders, full_path = self._disk.get_dir_list(self._fid)
                    info = {i.name: i for i in folders}
                    emit_infos['folder_list'] = {key: info.get(key) for key in sorted(info.keys())}  # {name-Folder}
                    emit_infos['path_list'] = full_path
            except TimeoutError:
                self.err_msg.emit("网络超时，无法更新目录，稍后再试！", 7000)
            except Exception as e:
                self.err_msg.emit("未知错误，无法更新目录，稍后再试！", 7000)
                logger.error(f"ListRefresher error: {e=}")
            else:
                self.infos.emit(emit_infos)
            self._is_work = False
            self._mutex.unlock()


class RemoveFilesWorker(QThread):
    '''删除文件(夹)线程'''
    msg = pyqtSignal(object, object)
    finished = pyqtSignal()

    def __init__(self, parent=None):
        super(RemoveFilesWorker, self).__init__(parent)
        self._disk = None
        self.infos = None
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, infos):
        self.infos = infos
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            if not self.infos:
                self._is_work = False
                self._mutex.unlock()
                return
            for i in self.infos:
                try:
                    self._disk.delete(i['fid'], i['is_file'])
                except TimeoutError:
                    self.msg.emit(f"删除 {i['name']} 因网络超时失败！", 3000)
                except Exception as e:
                    logger.error(f"RemoveFileWorker error: {e=}")
            self.finished.emit()
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行删除指令！", 3100)


class GetMoreInfoWorker(QThread):
    '''获取文件直链、文件(夹)提取码描述，用于登录后显示更多信息'''
    infos = pyqtSignal(object)
    share_url = pyqtSignal(object)
    dl_link = pyqtSignal(object)
    msg = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super(GetMoreInfoWorker, self).__init__(parent)
        self._disk = None
        self._infos = None
        self._url = ''
        self._pwd = ''
        self._emit_link= False
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, infos, emit_link=False):
        self._infos = infos
        self._emit_link= emit_link
        self.start()
    
    def get_dl_link(self, url, pwd):
        self._url = url
        self._pwd = pwd
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        # infos: ID/None，文件名，大小，日期，下载次数(dl_count)，提取码(pwd)，描述(desc)，|链接(share-url)
        if not self._is_work and self._infos:
            self._mutex.lock()
            self._is_work = True
            try:
                if not self._url:  # 获取普通信息
                    if isinstance(self._infos, (FolderInfos, FileInfos)):  # 从 disk 运行
                        self.msg.emit("网络请求中，请稍后……", 0)
                        _info = self._disk.get_share_info(self._infos.id, is_file=self._infos.is_file)
                        self._infos.desc = _info.desc
                        self._infos.pwd = _info.pwd
                        self._infos.url = _info.url
                        if self._emit_link:
                            self.share_url.emit(self._infos)
                        else:
                            self.infos.emit(self._infos)
                        self.msg.emit("", 0)  # 删除提示信息
                else:  # 获取下载直链
                    res = self._disk.get_file_info_by_url(self._url, self._pwd)
                    if res.code == LanZouCloud.SUCCESS:
                        self.dl_link.emit("{}".format(res.durl or "无"))  # 下载直链
                    elif res.code == LanZouCloud.NETWORK_ERROR:
                        self.dl_link.emit("网络错误！获取失败")  # 下载直链
                    else:
                        self.dl_link.emit("其它错误！")  # 下载直链
            except TimeoutError:
                self.msg.emit("网络超时！稍后重试", 6000)
            except Exception as e:
                logger.error(f"GetMoreInfoWorker error: {e=}")
            self._is_work = False
            self._url = ''
            self._pwd = ''
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class GetAllFoldersWorker(QThread):
    '''获取所有文件夹name与fid，用于文件移动'''
    infos = pyqtSignal(object, object)
    msg = pyqtSignal(str, int)
    moved = pyqtSignal(bool, bool, bool)

    def __init__(self, parent=None):
        super(GetAllFoldersWorker, self).__init__(parent)
        self._disk = None
        self.org_infos = None
        self._mutex = QMutex()
        self._is_work = False
        self.move_infos = None

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, org_infos):
        self.org_infos = org_infos  # 对话框标识文件与文件夹
        self.move_infos = None # 清除上次影响
        self.start()

    def move_file(self, info):
        '''移动文件至新的文件夹'''
        self.move_infos = info # file_id, folder_id, f_name, type(size)
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def move_file_folder(self, info:list, no_err:bool, r_files:bool, r_folders:bool):
        """移动文件(夹)"""
        # no_err 判断是否需要更新 UI
        fid = int(info[0])
        target_id = int(info[1])
        fname = info[2]
        is_file = True if info[3] else False
        if is_file:  # 文件
            if self._disk.move_file(fid, target_id) == LanZouCloud.SUCCESS:
                self.msg.emit(f"{fname} 移动成功！", 3000)
                no_err = True
                r_files = True
            else:
                self.msg.emit(f"移动文件{fname}失败！", 4000)
        else:  # 文件夹
            if self._disk.move_folder(fid, target_id) == LanZouCloud.SUCCESS:
                self.msg.emit(f"{fname} 移动成功！", 3000)
                no_err = True
                r_folders = True
            else:
                self.msg.emit(f"移动文件夹 {fname} 失败！移动的文件夹中不能包含子文件夹！", 4000)
        return no_err, r_files, r_folders

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            if self.move_infos:  # 移动文件
                no_err = False
                r_files = False
                r_folders = False
                for info in self.move_infos:
                    try:
                        no_err, r_files, r_folders = self.move_file_folder(info, no_err, r_files, r_folders)
                    except TimeoutError:
                        self.msg.emit(f"移动文件(夹) {info[2]} 失败，网络超时！请稍后重试", 5000)
                    except Exception as e:
                        logger.error(f"GetAllFoldersWorker error: {e=}")
                        self.msg.emit(f"移动文件(夹) {info[2]} 失败，未知错误！", 5000)
                if no_err:  # 没有错误就更新ui
                    sleep(2.1)  # 等一段时间后才更新文件列表
                    self.moved.emit(r_files, r_folders, False)
            else:  # 获取所有文件夹
                try:
                    self.msg.emit("网络请求中，请稍后……", 0)
                    all_dirs_dict = self._disk.get_move_folders().name_id
                    self.infos.emit(self.org_infos, all_dirs_dict)
                    self.msg.emit("", 0)  # 删除提示信息
                except TimeoutError:
                    self.msg.emit("网络超时！稍后重试", 6000)
                except Exception as e:
                    logger.error(f"GetAllFoldersWorker error: {e=}")
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class RenameMkdirWorker(QThread):
    """重命名、修改简介与新建文件夹 线程"""
    # infos = pyqtSignal(object, object)
    msg = pyqtSignal(str, int)
    update = pyqtSignal(object, object, object, object)

    def __init__(self, parent=None):
        super(RenameMkdirWorker, self).__init__(parent)
        self._disk = None
        self._work_id = -1
        self._folder_list = None
        self.infos = None
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, infos, work_id, folder_list):
        self.infos = infos  # 对话框标识文件与文件夹
        self._work_id = work_id
        self._folder_list = folder_list
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True

            action = self.infos[0]
            try:
                if action == 'new':  # 新建文件夹
                    new_name = self.infos[1]
                    new_des = self.infos[2]
                    if new_name in self._folder_list.keys():
                        self.msg.emit(f"文件夹已存在：{new_name}", 7000)
                    else:
                        res = self._disk.mkdir(self._work_id, new_name, new_des)
                        if res == LanZouCloud.MKDIR_ERROR:
                            self.msg.emit(f"创建文件夹失败：{new_name}", 7000)
                        else:
                            sleep(1.5)  # 暂停一下，否则无法获取新建的文件夹
                            self.update.emit(self._work_id, False, True, False)  # 此处仅更新文件夹，并显示
                            self.msg.emit(f"成功创建文件夹：{new_name}", 4000)
                else:  # 重命名、修改简介
                    has_file = False
                    has_folder = False
                    failed = False
                    for info in self.infos[1]:
                        if info.is_file:  # 修改文件描述
                            res = self._disk.set_desc(info.id, info.new_des, is_file=info.is_file)
                            if res == LanZouCloud.SUCCESS:
                                has_file = True
                            else:
                                failed = True
                        else:  # 修改文件夹，action == "folder"
                            name = info.new_name or info.nmae 
                            res = self._disk._set_dir_info(info.id, str(name), str(info.new_des))
                            if res == LanZouCloud.SUCCESS:
                                has_folder = True
                            else:
                                failed = True
                    self.update.emit(self._work_id, has_file, has_folder, False)
                    if failed:
                        self.msg.emit("有发生错误！", 6000)
                    else:
                        self.msg.emit("修改成功！", 4000)
            except TimeoutError:
                self.msg.emit("网络超时，请稍后重试！", 6000)
            except Exception as e:
                logger.error(f"RenameMikdirWorker error: {e=}")

            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class SetPwdWorker(QThread):
    '''设置文件(夹)提取码 线程'''
    msg = pyqtSignal(str, int)
    update = pyqtSignal(object, object, object, object)

    def __init__(self, parent=None):
        super(SetPwdWorker, self).__init__(parent)
        self._disk = None
        self.infos = []
        self._work_id = -1
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, infos, work_id):
        self.infos = infos
        self._work_id = work_id
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            try:
                has_file = False
                has_folder = False
                failed = False
                for infos in self.infos:
                    if infos.is_file:  # 文件
                        has_file = True
                        new_pwd = infos.new_pwd
                        if 2 > len(new_pwd) >= 1 or len(new_pwd) > 6:
                            self.msg.emit("文件提取码为2-6位字符,关闭请留空！", 4000)
                            raise UserWarning
                    else:  # 文件夹
                        has_folder = True
                        new_pwd = infos.new_pwd
                        if 2 > len(new_pwd) >= 1 or len(new_pwd) > 12:
                            self.msg.emit("文件夹提取码为0-12位字符,关闭请留空！", 4000)
                            raise UserWarning
                    res = self._disk.set_passwd(infos.id, infos.new_pwd, infos.is_file)
                    if res != LanZouCloud.SUCCESS:
                        failed = True
                if failed:
                    self.msg.emit("部分提取码变更失败❀╳❀:{}，请勿使用特殊符号!".format(res), 4000)
                else:
                    self.msg.emit("提取码变更成功！♬", 3000)

                self.update.emit(self._work_id, has_file, has_folder, False)
            except TimeoutError:
                self.msg.emit("网络超时，请稍后重试！", 6000)
            except UserWarning:
                pass
            except Exception as e:
                logger.error(f"SetPwdWorker error: {e=}")
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class LogoutWorker(QThread):
    '''获取所有文件夹name与fid，用于文件移动'''
    successed = pyqtSignal()
    msg = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super(LogoutWorker, self).__init__(parent)
        self._disk = None
        self.update_ui = True
        self._mutex = QMutex()
        self._is_work = False

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, update_ui=True):
        self.update_ui = update_ui
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            try:
                res = self._disk.logout()
                if res == LanZouCloud.SUCCESS:
                    if self.update_ui:
                        self.successed.emit()
                    self.msg.emit("已经退出登录！", 4000)
                else:
                    self.msg.emit("失败，请重试！", 5000)
            except TimeoutError:
                self.msg.emit("网络超时，请稍后重试！", 6000)
            except Exception as e:
                logger.error(f"LogoutWorker error: {e=}")
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class GetRecListsWorker(QThread):
    '''获取回收站列表'''
    folders = pyqtSignal(object)
    infos = pyqtSignal(object, object)
    msg = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super(GetRecListsWorker, self).__init__(parent)
        self._disk = None
        self._mutex = QMutex()
        self._is_work = False
        self._folder_id = None

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, fid):
        # 用于获取回收站指定文件夹内文件信息
        self._folder_id = fid
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            try:
                if self._folder_id:
                    file_lists = self._disk.get_rec_file_list(folder_id=self._folder_id)
                    self._folder_id = None
                    self.folders.emit(file_lists)
                    raise UserWarning
                dir_lists = self._disk.get_rec_dir_list()
                file_lists = self._disk.get_rec_file_list(folder_id=-1)
                self.infos.emit(dir_lists, file_lists)
                self.msg.emit("刷新列表成功！", 2000)
            except TimeoutError:
                self.msg.emit("网络超时，请稍后重试！", 6000)
            except UserWarning:
                pass
            except Exception as e:
                logger.error(f"GetRecListsWorker error: {e=}")
            self._is_work = False
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class RecManipulator(QThread):
    '''操作回收站'''
    msg = pyqtSignal(str, int)
    successed = pyqtSignal()

    def __init__(self, parent=None):
        super(RecManipulator, self).__init__(parent)
        self._disk = None
        self._mutex = QMutex()
        self._is_work = False
        self._action = None
        self._folders = []
        self._files= []

    def set_disk(self, disk):
        self._disk = disk

    def set_values(self, infos, action):
        # 操作回收站选定行
        self._action = None
        self._folders = []
        self._files= []
        for item in infos:
            if isinstance(item, RecFile):
                self._files.append(item.id)
            elif isinstance(item, RecFolder):
                self._folders.append(item.id)
        self._action = action
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            try:
                if self._action == "recovery":
                    if self._files or self._folders:
                        res = self._disk.recovery_multi(self._files, self._folders)
                        if res == LanZouCloud.SUCCESS:
                            self.msg.emit("选定文件(夹)恢复成功，即将刷新列表", 2500)
                elif self._action == "delete":
                    if self._files or self._folders:
                        if self._files or self._folders:
                            res = self._disk.delete_rec_multi(self._files, self._folders)
                            if res == LanZouCloud.SUCCESS:
                                self.msg.emit("选定文件(夹)彻底删除成功，即将刷新列表", 2500)
                elif self._action == "clean":
                    res = self._disk.clean_rec()
                    if res == LanZouCloud.SUCCESS:
                        self.msg.emit("清空回收站成功，即将刷新列表", 2500)
                elif self._action == "recovery_all":
                    res = self._disk.recovery_all()
                    if res == LanZouCloud.SUCCESS:
                        self.msg.emit("文件(夹)全部还原成功，即将刷新列表", 2500)
                if isinstance(res, int):
                    if res == LanZouCloud.FAILED:
                        self.msg.emit("失败，请重试！", 4500)
                    elif res == LanZouCloud.NETWORK_ERROR:
                        self.msg.emit("网络错误，请稍后重试！", 4500)
                    else:
                        sleep(2.6)
                        self.successed.emit()
            except TimeoutError:
                self.msg.emit("网络超时，请稍后重试！", 6000)
            except Exception as e:
                logger.error(f"RecManipulator error: {e=}")
            self._is_work = False
            self._action = None
            self._folders = []
            self._files= []
            self._mutex.unlock()
        else:
            self.msg.emit("后台正在运行，请稍后重试！", 3100)


class CheckUpdateWorker(QThread):
    '''检测软件更新'''
    infos = pyqtSignal(object, object)
    bg_update_infos = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super(CheckUpdateWorker, self).__init__(parent)
        self._ver = ''
        self._manual = False
        self._mutex = QMutex()
        self._is_work = False
        self._folder_id = None
        self._api = 'https://api.github.com/repos/rachpt/lanzou-gui/releases/latest'
        self._api_mirror = 'https://gitee.com/api/v5/repos/rachpt/lanzou-gui/releases/latest'

    def set_values(self, ver: str, manual: bool=False):
        # 检查更新
        self._ver = ver
        self._manual = manual
        self.start()

    def __del__(self):
        self.wait()

    def stop(self):
        self._mutex.lock()
        self._is_work = False
        self._mutex.unlock()

    def run(self):
        if not self._is_work:
            self._mutex.lock()
            self._is_work = True
            resp = None
            try:
                resp = requests.get(self._api).json()
            except (requests.RequestException, TimeoutError, requests.exceptions.ConnectionError):
                logger.debug("chcek update from github error")
                try: resp = requests.get(self._api_mirror).json()
                except:
                    logger.debug("chcek update from gitee error")
            except Exception as e:
                logger.error(f"CheckUpdateWorker error: {e=}")
            if resp:
                try:
                    tag_name, msg = resp['tag_name'], resp['body']
                    ver = self._ver.replace('v', '').split('-')[0].split('.')
                    ver2 = tag_name.replace('v', '').split('-')[0].split('.')
                    local_version = int(ver[0]) * 100 + int(ver[1]) * 10 + int(ver[2])
                    remote_version = int(ver2[0]) * 100 + int(ver2[1]) * 10 + int(ver2[2])
                    if remote_version > local_version:
                        urls = re.findall(r'https?://[-\.a-zA-Z0-9/_#?&%@]+', msg)
                        for url in urls:
                            new_url = f'<a href="{url}">{url}</a>'
                            msg = msg.replace(url, new_url)
                        msg = msg.replace('\n', '<br />')
                        self.infos.emit(tag_name, msg)
                        if not self._manual:  # 打开软件时检测更新
                            self.bg_update_infos.emit(tag_name, msg)
                    elif self._manual:
                        self.infos.emit("0", "目前还没有发布新版本！")
                except AttributeError:
                    if self._manual:
                        self.infos.emit("v0.0.0", "检查更新时发生异常，请重试！")
                except Exception as e:
                    logger.error(f"Check Update Version error: {e=}")
            else:
                if self._manual:
                    self.infos.emit("v0.0.0", f"检查更新时 <a href='{self._api}'>api.github.com</a>、<a href='{self._api_mirror}'>gitee.com</a> 拒绝连接，请稍后重试！")
            self._manual = False
            self._is_work = False
            self._mutex.unlock()
        else:
            if self._manual:
                self.infos.emit("v0.0.0", "后台正在运行，请稍等！")
