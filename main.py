import main_upload
import signal
from utility import tool
import sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler import events


job = BlockingScheduler(logger=tool.getLogger())


def exits(signalNum, frame):
    # print(signalNum, frame)
    logger = tool.getLogger()
    logger.info("等待任务正常结束，请勿强制关闭，避免出现数据丢失！！！")
    tool.settingConf.save()
    job.shutdown()
    sys.exit(0)


def handleException(exp):
    logger = tool.getLogger()
    logger.error(str(exp.exception))
    exp.print_stack()


signal.signal(signal.SIGINT, exits)
signal.signal(signal.SIGTERM, exits)

# 截取失败记录，方便debug
job.add_listener(handleException, events.EVENT_JOB_ERROR)

# 获取视频任务定时任务
job.add_job(main_upload.jobProducer, **tool.settingConf["Scheduler"]["Video"])

# 定时确定视频消费者线程是否正常运行
job.add_job(main_upload.jobConsumer, trigger="interval", minutes=2)

# 字幕的定时任务暂时不导入
# implement

# 开启定时任务
job.start()
