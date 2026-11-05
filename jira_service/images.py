"""Bounded screenshot inputs. Raster bytes stay immutable and hash-addressed."""
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from .materials import sha256

FORMATS={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}
MAX_BYTES=8*1024*1024


def inspect_image(path):
    path=Path(path)
    if not 0<path.stat().st_size<=MAX_BYTES:raise ValueError('截图应小于8MiB')
    try:
        with Image.open(path,formats=list(FORMATS)) as image:
            width,height=image.size;fmt=image.format
            if width*height>16_000_000 or min(width,height)<1:raise ValueError('截图像素超出1600万上限')
            if getattr(image,'n_frames',1)!=1:raise ValueError('动态图暂不进入截图分析')
            image.verify()
        with Image.open(path,formats=list(FORMATS)) as image:image.load()
    except (UnidentifiedImageError,OSError,SyntaxError,Image.DecompressionBombError) as exc:
        raise ValueError('截图损坏或格式不支持（仅PNG/JPEG/WebP）') from exc
    return {'format':fmt,'media_type':FORMATS[fmt],'width':width,'height':height,'sha256':sha256(path)}


def curated_images(settings,store,key,limit=2):
    images=[];skipped=[]
    for artifact in store.artifacts(key):
        if artifact['kind']=='image_unavailable':
            skipped.append({'artifact_id':artifact['id'],'reason':artifact['metadata'].get('image_error','图片未解码')})
            continue
        if artifact['kind']!='image':continue
        if len(images)>=limit:
            skipped.append({'artifact_id':artifact['id'],'reason':'每轮最多分析2张截图，其余未查看'})
            continue
        path=Path(store.rows('SELECT path FROM artifacts WHERE id=?',(artifact['id'],))[0]['path']).resolve()
        path.relative_to(settings.data)
        metadata=inspect_image(path)
        if metadata['sha256']!=artifact['metadata'].get('sha256'):
            raise ValueError('截图SHA256不一致，请重新下载或导入材料')
        images.append({'artifact_id':artifact['id'],'filename':artifact['filename'],'path':str(path),**metadata})
    return images,skipped
