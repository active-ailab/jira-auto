"""Configure a dedicated bot without putting secrets in shell history or chat."""
import argparse
import getpass
import os
import secrets
import subprocess
from urllib.parse import urlparse
from jira_service.settings import Settings
from jira_service.feishu import save_config, init_store, load_config
from jira_service.store import Store


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--show-pairing',action='store_true',help='仅在本机显示当前绑定口令，不改配置')
    args=parser.parse_args()
    settings=Settings.load()
    if args.show_pairing:
        conf=load_config(settings)
        if conf.get('open_id'): print('机器人已绑定。'); return
        if not conf.get('pairing_token'): raise SystemExit('尚未配置应用，请先运行配置脚本')
        print('请把下面这一整行私聊发送给机器人：\n')
        print('绑定 jira-auto '+conf['pairing_token'])
        return
    print('创建/配置飞书企业自建应用，开启机器人能力。凭据仅保存到本机权限 600 的配置文件。')
    app_id=input('App ID: ').strip()
    secret=getpass.getpass('App Secret（隐藏输入）: ').strip()
    default_url=os.getenv('JA_DASHBOARD_URL','http://127.0.0.1:8791')
    url=input(f'看板地址 [{default_url}]: ').strip() or default_url
    parsed=urlparse(url)
    if not app_id.startswith('cli_') or not secret or parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise SystemExit('配置格式无效，未保存')
    token=secrets.token_urlsafe(18)
    subprocess.run(['systemctl','--user','stop','jira-auto.service'],check=True)
    store=Store(settings.data);init_store(store)
    with store.db() as c:
        c.execute("UPDATE feishu_deliveries SET state='reconfigured' WHERE state IN ('sent','queued')")
    save_config(settings,{'app_id':app_id,'app_secret':secret,'dashboard_url':url.rstrip('/'),'pairing_token':token})
    subprocess.run(['systemctl','--user','restart','jira-auto.service'],check=True)
    print('\n在飞书开发者后台开通机器人发送消息、接收单聊消息权限。')
    print('事件与回调：启用长连接，添加 im.message.receive_v1 和 card.action.trigger，发布应用并配置可用范围。')
    print('\n在飞书中私聊机器人发送以下完整口令（不要发到群里）：\n')
    print('绑定 jira-auto '+token)
    print('\n收到绑定成功消息后，待审分析会自动发送。旧卡片在重新配置后不能用于审批。')

if __name__=='__main__': main()
