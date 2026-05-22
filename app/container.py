from functools import lru_cache

from app.agents import ChannelCsAgent, MainAgent
from app.audit import AuditLogger
from app.channels import KakaoBizCenterClient, NaverTalkTalkClient
from app.config import get_settings
from app.llm import ChatGPTClient
from app.models import ChannelName
from app.storage import CsRepository


@lru_cache
def get_repository() -> CsRepository:
    settings = get_settings()
    repository = CsRepository(settings.sqlite_path)
    repository.initialize()
    return repository


@lru_cache
def get_main_agent() -> MainAgent:
    settings = get_settings()
    repository = get_repository()
    llm = ChatGPTClient(settings)
    audit_logger = AuditLogger(settings.audit_log_dir)
    kakao_agent = ChannelCsAgent(KakaoBizCenterClient(settings), llm, repository)
    naver_agent = ChannelCsAgent(NaverTalkTalkClient(settings), llm, repository)
    return MainAgent(
        sub_agents={
            ChannelName.KAKAO: kakao_agent,
            ChannelName.NAVER: naver_agent,
        },
        repository=repository,
        llm=llm,
        audit_logger=audit_logger,
    )
