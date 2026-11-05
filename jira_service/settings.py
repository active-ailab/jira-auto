import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data: Path
    source: Path
    port: int = 8791
    poll_seconds: int = 300
    jira_write: bool = False
    jira_bin: str = "jira"
    elf_bin: str = "elf-fetch"
    host: str = "127.0.0.1"
    lan_auth: bool = False
    ai_enabled: bool = True
    codex_bin: str = "codex"
    ai_proxy: str = ""
    ai_timeout: int = 600
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")

    @classmethod
    def load(cls):
        return cls(
            data=Path(os.getenv("JA_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))).resolve(),
            source=Path(os.getenv("JA_SOURCE_DIR", str(Path(__file__).resolve().parents[2]))).resolve(),
            port=int(os.getenv("JA_PORT", "8791")),
            poll_seconds=max(60, int(os.getenv("JA_POLL_SECONDS", "300"))),
            jira_write=os.getenv("JA_ALLOW_JIRA_WRITE") == "1",
            jira_bin=os.getenv("JA_JIRA_BIN", "jira"),
            elf_bin=os.getenv("JA_ELF_BIN", "elf-fetch"),
            host=os.getenv("JA_HOST", "127.0.0.1"),
            lan_auth=os.getenv("JA_LAN_AUTH") == "1",
            ai_enabled=os.getenv('JA_AI_ENABLED','1') == '1',
            codex_bin=os.getenv('JA_CODEX_BIN','codex'),
            ai_proxy=os.getenv('JA_AI_PROXY',''),
            ai_timeout=max(30,int(os.getenv('JA_AI_TIMEOUT','600'))),
            allowed_hosts=tuple(x.strip() for x in os.getenv("JA_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if x.strip()),
        )
