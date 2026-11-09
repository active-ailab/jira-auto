import hashlib
import io
import json
import re
import stat
import struct
import uuid
import zipfile
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

from .integrations import DEVICE, VERSION, digest, run_cli


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_manifest(path):
    # Reject entity declarations before using the stdlib XML parser.
    b = Path(path).read_bytes()
    if len(b) > 4 * 1024 * 1024 or b"<!DOCTYPE" in b.upper() or b"<!ENTITY" in b.upper():
        raise ValueError("manifest 过大或包含不支持的实体声明")
    root = ET.fromstring(b)
    if root.tag != "manifest":
        raise ValueError("不是 repo manifest")
    projects = root.findall("project")
    return {"projects": len(projects), "pinned_projects": sum(
        bool(re.fullmatch(r"[0-9a-fA-F]{40}", x.get("revision", ""))) for x in projects),
        "includes": [x.get("name") for x in root.findall("include")], "sha256": sha256(path)}


def inspect_elf(path):
    with Path(path).open("rb") as f:
        h = f.read(64)
    if h[:4] != b"\x7fELF" or h[4] not in (1, 2) or h[5] not in (1, 2):
        raise ValueError("文件不是有效 ELF")
    endian = "<" if h[5] == 1 else ">"
    machine = struct.unpack_from(endian + "H", h, 18)[0]
    sections = run_cli(["readelf", "-S", str(path)], timeout=60)
    notes = run_cli(["readelf", "-n", str(path)], timeout=60)
    build_id = re.search(r"Build ID:\s*(\w+)", notes)
    return {"architecture": {243: "RISC-V", 40: "ARM", 183: "AArch64"}.get(machine, str(machine)),
            "bits": 32 if h[4] == 1 else 64, "debug_info": ".debug_info" in sections,
            "debug_line": ".debug_line" in sections, "symbols": ".symtab" in sections,
            "build_id": build_id[1] if build_id else None, "sha256": sha256(path)}


# Verified CI naming: GenevaW and GenevaWN use Geneva build 125391 (3.25.0.1).
# GenevaWN's associated dump matches the candidate ELF's main .text bytes.
# This alias selects artifacts only. Scene/version/code-byte checks still apply.
def build_device(device):
    return {'GenevaW':'Geneva','GenevaWN':'Geneva'}.get(device,device)


def fetch_artifacts(settings, store, key, device, version, run_id):
    if not DEVICE.fullmatch(device or "") or not VERSION.fullmatch(version or ""):
        raise ValueError("缺少有效设备名或版本号，请先核对工单字段")
    output = settings.data / "cases" / key / "builds"
    output.mkdir(parents=True, exist_ok=True)
    # Keep corrupted cached outputs for diagnosis, but don't let elf-fetch reuse them by filename.
    for old in store.artifacts(key):
        if old['kind'] not in {'elf','map','manifest'} or old['metadata'].get('device')!=device or old['metadata'].get('version')!=version:continue
        old_path=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(old['id'],))[0]['path']).resolve()
        old_path.relative_to(output.resolve())
        if old_path.is_file() and sha256(old_path)!=old['metadata'].get('sha256'):
            old_path.rename(old_path.with_name(old_path.name+'.invalid-'+str(uuid.uuid4())))
            store.event('validate','已隔离损坏的版本缓存，重新下载：'+old_path.name,key,run_id)
    store.event("fetch", f"获取 {device} / {version} 的 XML、ELF、MAP", key, run_id)
    resolved_device=build_device(device)
    if resolved_device!=device:store.event("fetch",f"构建机型映射：质量平台 {device} → CI {resolved_device}；仍需现场与ELF校验",key,run_id)
    run_cli([settings.elf_bin, "--device", resolved_device, "--version", version,
             "--files", "xml,elf,map", "--dir", str(output)], timeout=1800,
            on_output=lambda line: store.event('tool', line, key, run_id))
    target = output / resolved_device / version
    elfs=list(target.glob('watch@*.elf'))
    if len(elfs)!=1:raise ValueError('版本目录必须有唯一 watch@*.elf；请核对 elf-fetch 的板型产物')
    files = [("manifest", target / "manifest.xml"), ("elf", elfs[0]), ("map", elfs[0].with_suffix('.map'))]
    result = []
    for kind, path in files:
        store.event('validate', f'正在校验 {path.name}', key, run_id)
        if not path.is_file():
            raise RuntimeError(f"elf-fetch 未生成预期文件：{path.name}")
        path.resolve().relative_to(settings.data)
        meta = {"device": device, "build_device":resolved_device,"version": version, "bytes": path.stat().st_size}
        meta.update(inspect_manifest(path) if kind == "manifest" else inspect_elf(path)
                    if kind == "elf" else {"sha256": sha256(path)})
        aid = store.artifact(key, kind, path, meta)
        store.event('validate', f'{path.name} 校验完成', key, run_id,
                    details={'artifact_id':aid,'kind':kind,**meta})
        result.append({"id": aid, "kind": kind, **meta})
    store.event("fetch", "构建产物已校验；尚未证明现场与 ELF 的精确构建匹配", key, run_id)
    return {"files": result, "exact_build_match": "unverified"}


def fetch_manifest(settings,store,key,device,version,run_id):
    if not DEVICE.fullmatch(device or '') or not VERSION.fullmatch(version or ''):raise ValueError('重启分析缺少有效设备/版本')
    output=settings.data/'cases'/key/'builds'
    store.event('fetch','重启类型分析：只获取目标版本XML以核对按键与复位源码',key,run_id)
    run_cli([settings.elf_bin,'--device',build_device(device),'--version',version,'--files','xml','--dir',str(output)],timeout=300,
            on_output=lambda line:store.event('tool',line,key,run_id))
    path=output/build_device(device)/version/'manifest.xml';info=inspect_manifest(path)
    aid=store.artifact(key,'manifest',path,{'device':device,'version':version,'bytes':path.stat().st_size,**info})
    return {'artifact_id':aid,'mode':'manifest_only','version':version}


def unpack_logs(archive, target, limit=512 * 1024 * 1024):
    """Bounded extraction with no archive-controlled filesystem paths."""
    found = []
    used = 0
    count = 0

    def visit(z, lineage, depth):
        nonlocal used, count
        for entry in z.infolist():
            if entry.is_dir():
                continue
            count += 1
            if count > 5000:
                raise ValueError("日志包文件数量过多")
            name = PurePosixPath(entry.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or re.match(r"^[A-Za-z]:", str(name)):
                raise ValueError("日志包包含不安全路径")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("日志包包含符号链接")
            used += entry.file_size
            if used > limit:
                raise ValueError("日志解压大小超限")
            if entry.flag_bits & 1:
                raise ValueError("ZIP 自身有密码保护，需另行处理")
            b = z.read(entry)  # zipfile verifies CRC during this read.
            label = lineage + [entry.filename]
            if zipfile.is_zipfile(io.BytesIO(b)):
                if depth >= 4:
                    raise ValueError("日志嵌套层数超限")
                with zipfile.ZipFile(io.BytesIO(b)) as inner:
                    visit(inner, label, depth + 1)
            else:
                filename = f"{len(found):04d}-" + re.sub(r"[^A-Za-z0-9._-]", "_", name.name)[:160]
                found.append((filename, label, b))

    with zipfile.ZipFile(archive) as z:
        visit(z, [], 0)
    target.mkdir(parents=True, exist_ok=False)
    results = []
    for filename, label, b in found:
        p = target / filename
        p.write_bytes(b)
        from .material_probe import inspect_bytes
        probe=inspect_bytes(b,p.name)
        results.append((p, {"original_path": " / ".join(label), "bytes": len(b),
                            "sha256": hashlib.sha256(b).hexdigest(),**probe}))
    return results


def import_logs(settings, store, key, path, run_id, source_meta=None):
    archive = Path(path).resolve()
    archive.relative_to(settings.data)
    target = settings.data / "cases" / key / "logs" / str(uuid.uuid4())
    store.event("logs", "开始校验和展开日志包", key, run_id)
    files = unpack_logs(archive, target)
    if not files:raise ValueError('日志包内没有可导入文件')
    for p, meta in files:
        meta.update({'source_archive':archive.name,'archive_sha256':sha256(archive),**(source_meta or {})})
        kind='log' if meta['readable_text'] else 'binary_log'
        if p.suffix.lower() in ('.png','.jpg','.jpeg','.webp'):
            from .images import inspect_image
            try:meta.update(inspect_image(p));kind='image'
            except ValueError as exc:meta['image_error']=str(exc);kind='image_unavailable'
        aid = store.artifact(key,kind,p,meta)
        store.event('materials', f"已登记 {p.name}："+({'log':'可读文本','image':'截图，可供模型查看','image_unavailable':'截图未解码：'+meta.get('image_error','')}.get(kind,meta.get('analysis_value','二进制材料')+'；'+meta.get('inspection',''))), key, run_id,
                    details={'artifact_id':aid,**meta})
    store.artifact(key, "log_archive", archive, {"bytes": archive.stat().st_size, "sha256": sha256(archive), **(source_meta or {})})
    store.event("logs", f"已导入 {len(files)} 个文件", key, run_id)
    return {"files": len(files)}


def log_evidence(store, key, query=None, limit=30, emit=None, scope=None):
    rows = store.rows("SELECT * FROM artifacts WHERE issue_key=? AND kind='log' ORDER BY created DESC", (key,))
    evidence = []
    versions = []
    errors = []
    summaries = []
    seen = set()
    active_ids={a['id'] for a in store.artifacts(key)}
    selected_ids={scene['artifact_id'] for scene in scope} if scope else None
    for artifact in rows:
        if artifact['id'] not in active_ids:continue
        if selected_ids is not None and artifact['id'] not in selected_ids:continue
        metadata = json.loads(artifact['metadata'])
        identity = metadata.get('sha256') or artifact['id']
        if identity in seen:
            continue
        seen.add(identity)
        p = Path(artifact["path"]).resolve()
        p.relative_to(store.data)
        if not p.exists():
            continue
        line_num, scanned, file_versions, file_errors = 0, 0, [], 0
        total = p.stat().st_size
        if emit:
            emit('scan', f'开始读取 {p.name}（{total:,} 字节）', {'artifact_id':artifact['id'],'current':0,'total':total,'unit':'bytes'})
        with p.open('rb') as f:
            for line_num, rawline in enumerate(f, 1):
                scanned += len(rawline)
                if scope and not any(s['artifact_id']==artifact['id'] and s['line_start']<=line_num<=s['line_end'] for s in scope):continue
                line = rawline.decode('utf-8', errors='replace')
                # Version contexts explicitly exclude OTA target versions.
                m = re.search(r"firmware version (?:from /mnt/system/res_info:|=)\s*([\d.]+)", line)
                if m:
                    candidate = {"artifact_id": artifact["id"], "line": line_num, "version": m[1], "text": line.strip()[:600]}
                    versions.append(candidate)
                    file_versions.append(candidate)
                    if emit:
                        emit('evidence', f'运行版本证据：{m[1]}，第 {line_num:,} 行', candidate)
                if query and query.casefold() in line.casefold():
                    evidence.append({"artifact_id": artifact["id"], "line": line_num, "text": line.strip()[:1200]})
                    if len(evidence) >= limit:
                        return {"matches": evidence, "truncated": True}
                if not query and not re.search(r'reset reason\s*\{',line,re.I) and re.search(r"\b(panic|assert(?:ion)?|watchdog|hardfault|exception|ramdump)\b", line, re.I):
                    file_errors += 1
                    errors.append({"artifact_id": artifact["id"], "line": line_num, "text": line.strip()[:800]})
                    if len(errors) > 100:
                        errors = errors[-30:]
                if emit and line_num % 100000 == 0:
                    emit('scan', f'{p.name}：已扫描 {line_num:,} 行，{scanned:,}/{total:,} 字节',
                         {'artifact_id':artifact['id'],'current':scanned,'total':total,'unit':'bytes','lines':line_num})
        summary = {'artifact_id':artifact['id'],'filename':p.name,'lines':line_num,'bytes':scanned,
                   'last_runtime_version':file_versions[-1]['version'] if file_versions else None,
                   'version_records':len(file_versions),'exception_candidates':file_errors}
        summaries.append(summary)
        if emit:
            emit('scan', f'{p.name} 读取完成：{line_num:,} 行；运行版本记录 {len(file_versions)} 条；异常关键词候选 {file_errors} 条',
                 {**summary,'current':scanned,'total':total,'unit':'bytes'})
    if query:
        return {"matches": evidence, "truncated": False}
    return {"versions": versions[-30:], "exception_candidates": errors[-30:], 'files':summaries,
            "note": "关键词命中只是候选证据，不能据此判定根因或故障时间"}


def prepare_report(store, key, emit=None):
    emit = emit or (lambda *args: None)
    issue = store.issue(key)
    emit('context', f"读取问题：{issue.get('symptom') or issue['summary']}",
         {'device':issue.get('device'),'firmware':issue.get('firmware'),'feedback_time':issue.get('feedback_time')})
    artifacts = store.artifacts(key)
    emit('materials', f'检查已入库材料，共 {len(artifacts)} 份',
         {'files':[{'id':a['id'],'name':a['filename'],'kind':a['kind'],'bytes':a['metadata'].get('bytes')} for a in artifacts]})
    if not any(a['kind']=='log' for a in artifacts):
        emit('blocked', '缺少可读日志：请打开质量平台下载原始日志 ZIP 后导入；目前不能开展日志分析', None)
    emit('scope', '开始材料预检：提取运行版本和异常候选，完成后进入 AI 分析', None)
    evidence = log_evidence(store, key, emit=emit)
    versions = evidence["versions"]
    last_versions = {x['last_runtime_version'] for x in evidence['files'] if x['last_runtime_version']}
    last_version = next(iter(last_versions)) if len(last_versions)==1 else None
    emit('validate', f"版本核对：反馈 {issue.get('firmware') or '缺失'}；日志末次运行版本 {last_version or '无法唯一确定'}",
         {'feedback_version':issue.get('firmware'),'log_last_versions':sorted(last_versions),
          'matched':bool(last_version and last_version==issue.get('firmware'))})
    for candidate in evidence['exception_candidates'][-8:]:
        emit('evidence', f"异常关键词候选，第 {candidate['line']:,} 行（尚非根因）", candidate)
    emit('conclusion', '材料预检结束：故障时间段、精确 ELF 构建匹配及根因仍待验证',
         {'confirmed':['材料清单','日志运行版本记录'],'unverified':['故障对应时间段','现场与 ELF 的精确构建身份','根因'],
          'model_analysis':'pending'})
    lines = [f"预处理报告：{key}", "", f"问题：{issue['summary']}",
             f"反馈设备 / 固件：{issue.get('device') or '待确认'} / {issue.get('firmware') or '待确认'}",
             "", "已确认材料："]
    for kind, title in [("log", "可读日志"), ("binary_log", "二进制材料"), ("manifest", "Manifest"), ("elf", "ELF")]:
        lines.append(f"- {title}：{sum(x['kind'] == kind for x in artifacts)} 个")
    lines += [f"- 日志最后观察到的运行版本：{last_version or '未提取到'}"]
    if versions:
        last = versions[-1]
        lines.append(f"  证据：文件 {last['artifact_id']}，第 {last['line']} 行：{last['text']}")
    if last_version and issue.get("firmware") and last_version != issue["firmware"]:
        lines.append("- 版本冲突：日志末次运行版本与反馈不一致，禁止据此定版分析。")
    if len(last_versions)>1:
        lines.append('- 多份日志的末次运行版本不一致：'+', '.join(sorted(last_versions))+'；需先选择与故障对应的日志。')
    lines += ["", "分析边界：", "- 这是规则提取的材料清单，尚未执行模型根因分析。",
              "- 日志可能覆盖多次启动和升级；需先确认用户故障时间段。",
              "- ELF 与故障现场的精确构建身份尚未校验。",
              f"- 异常关键词候选（最多保留 30 条）：{len(evidence['exception_candidates'])} 条；不能直接作为根因。",
              "", "下一步：核对故障时间、截图、调用栈及对应源码后，补充人工或模型分析。"]
    return "\n".join(lines), evidence
