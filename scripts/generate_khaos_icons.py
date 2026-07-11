#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KHAOS 品牌视觉资源生成器 v6.0 (华尔街机构级终极版)
============================================================
功能: 一键生成 favicon.ico、Apple Touch Icon、各类 Logo (含 maskable)、
      快捷方式图标、4K/移动端截图、iOS 启动画面。
      自动生成资源清单与校验和，满足金融级部署审计要求。
依赖: Pillow >= 9.0.0
版权: Copyright (c) 2026 KHAOS Engineering. All rights reserved.
历史版本:
  v4.0 - 增加 maskable 安全区适配
  v5.0 - 原子保存与增强错误处理
  v6.0 - 全面机构级审计，完善隐私、资源管理与跨平台兼容
示例: python generate_khaos_icons.py --output-dir ../frontend/public/icons/
============================================================
"""
import os, sys, time, shutil, hashlib, json, logging, signal, argparse, platform, tempfile
from pathlib import Path
from typing import Dict, List, Optional
from functools import lru_cache

# ---------------------------------------------------------------------------
# 全局品牌常量
# ---------------------------------------------------------------------------
BRAND_NAME = "KHAOS"
GOLD = (232, 193, 112)
GOLD_ALPHA = (232, 193, 112, 255)
DARK = (10, 14, 23)
DARK_ALPHA = (10, 14, 23, 255)
TRANSPARENT = (0, 0, 0, 0)
SHIELD_INNER = (180, 150, 80, 80)
SHORTCUT_BG = (0, 0, 0, 180)
VERSION = "6.0.0"
MIN_PILLOW_VERSION = "9.0.0"

# ---------------------------------------------------------------------------
# 日志配置 (高精度时间戳)
# ---------------------------------------------------------------------------
LOG_FORMAT = '%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 信号处理 (保存原有处理器)
# ---------------------------------------------------------------------------
_original_sigint = signal.getsignal(signal.SIGINT)
def handle_interrupt(signum, frame):
    logger.warning("用户中断操作，正在退出...")
    if callable(_original_sigint):
        _original_sigint(signum, frame)
    sys.exit(1)
signal.signal(signal.SIGINT, handle_interrupt)

# ---------------------------------------------------------------------------
# 依赖检查与全局导入
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont
    pillow_version = Image.__version__
    if pillow_version < MIN_PILLOW_VERSION:
        logger.critical(f"需要 Pillow >= {MIN_PILLOW_VERSION}，当前版本: {pillow_version}")
        sys.exit(1)
except ImportError:
    logger.critical("缺少必要的 Pillow 库。请执行: pip install Pillow")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 字体缓存查找
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def find_font(size: int) -> ImageFont.FreeTypeFont:
    """尝试找到合适的字体，否则返回默认字体"""
    candidate_fonts = [
        "Arial.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\Arial.ttf",
    ]
    for font_path in candidate_fonts:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            continue
    logger.warning("未找到系统字体，将使用默认字体。某些文字可能极小，建议安装常用字体。")
    return ImageFont.load_default()

# ---------------------------------------------------------------------------
# 内部绘图函数
# ---------------------------------------------------------------------------
def _create_image(size: int, mode: str = 'RGBA') -> Image.Image:
    if size <= 0:
        raise ValueError(f"size 必须大于 0，实际: {size}")
    if mode == 'RGBA':
        return Image.new(mode, (size, size), TRANSPARENT)
    else:
        return Image.new(mode, (size, size), DARK)

def _draw_shield(draw: ImageDraw.Draw, cx: float, cy: float, r: float, with_shadow: bool = True):
    pts = [
        (cx - r * 0.9, cy - r * 1.1),
        (cx + r * 0.9, cy - r * 1.1),
        (cx + r * 0.7, cy + r * 0.3),
        (cx, cy + r * 1.2),
        (cx - r * 0.7, cy + r * 0.3)
    ]
    draw.polygon(pts, fill=GOLD_ALPHA, outline=DARK_ALPHA)
    if with_shadow and r > 20:
        inner_pts = [
            (cx - r * 0.75, cy - r * 1.0),
            (cx + r * 0.75, cy - r * 1.0),
            (cx + r * 0.6, cy + r * 0.25),
            (cx, cy + r * 1.1),
            (cx - r * 0.6, cy + r * 0.25)
        ]
        draw.polygon(inner_pts, fill=None, outline=SHIELD_INNER)

def _draw_letter_K(draw: ImageDraw.Draw, cx: float, cy: float, r: float):
    lw = max(1, int(r * 0.3))
    draw.line([(cx - r * 0.35, cy - r * 0.7),
               (cx - r * 0.35, cy + r * 0.7)], fill=DARK_ALPHA, width=lw)
    draw.line([(cx - r * 0.35, cy - r * 0.1),
               (cx + r * 0.55, cy - r * 0.75)], fill=DARK_ALPHA, width=lw)
    draw.line([(cx - r * 0.35, cy + r * 0.1),
               (cx + r * 0.55, cy + r * 0.65)], fill=DARK_ALPHA, width=lw)

# ---------------------------------------------------------------------------
# 图标生成（包含 maskable 安全区适配）
# ---------------------------------------------------------------------------
def make_icon(size: int, with_shadow: bool = True, maskable: bool = False) -> Image.Image:
    if size < 12:
        raise ValueError("图标最小尺寸为 12x12")
    if maskable:
        img = Image.new('RGBA', (size, size), DARK_ALPHA)
        with_shadow = False
        r_safe = size * 0.34  # 安全区适配
    else:
        img = _create_image(size, 'RGBA')
        margin = size * 0.1
        draw_bg = ImageDraw.Draw(img)
        draw_bg.ellipse([margin, margin, size - margin, size - margin], fill=DARK_ALPHA)
        r_safe = size * 0.38
    draw = ImageDraw.Draw(img)
    cx = size / 2
    cy = size / 2
    _draw_shield(draw, cx, cy, r_safe, with_shadow)
    _draw_letter_K(draw, cx, cy, r_safe)
    img.info['dpi'] = (144, 144)
    img.info['Copyright'] = f"Copyright (c) 2026 KHAOS Engineering."
    img.info['Software'] = f"KHAOS Icon Generator v{VERSION}"
    return img

# ---------------------------------------------------------------------------
# 快捷方式图标
# ---------------------------------------------------------------------------
def make_shortcut_icon(size: int, label: str) -> Image.Image:
    img = make_icon(size, with_shadow=True)
    if not label:
        return img
    draw = ImageDraw.Draw(img)
    font_size = int(size * 0.25)
    font = None
    while font_size > 0:
        try:
            font = find_font(font_size)
        except Exception:
            font_size -= 1
            continue
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        if tw <= size * 0.8:
            break
        font_size -= 1
    if font is None or font_size <= 0:
        return img
    th = bbox[3] - bbox[1]
    padding = 2
    x = size - tw - padding
    y = size - th - padding
    draw.rectangle([x - 2, y - 2, x + tw + 2, y + th + 2], fill=SHORTCUT_BG)
    draw.text((x, y), label, fill=GOLD, font=font)
    img.info['dpi'] = (144, 144)
    return img

# ---------------------------------------------------------------------------
# 启动画面 / 截图
# ---------------------------------------------------------------------------
def make_splash(width: int, height: int, logo_scale: float = 0.25,
                add_text: bool = True, font_size_ratio: float = 0.04) -> Image.Image:
    if logo_scale <= 0 or logo_scale >= 1:
        raise ValueError("logo_scale 应该在 (0,1) 之间")
    img = Image.new('RGB', (width, height), DARK)
    draw = ImageDraw.Draw(img)
    min_dim = min(width, height)
    logo_size = int(min_dim * logo_scale)
    logo_size = max(12, min(logo_size, int(min_dim * 0.9)))
    logo = make_icon(logo_size, with_shadow=True)
    logo_x = (width - logo_size) // 2
    logo_y = (height - logo_size) // 2 - int(logo_size * 0.15)
    img.paste(logo, (logo_x, logo_y), logo)
    if add_text:
        font = find_font(int(height * font_size_ratio))
        text = BRAND_NAME
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        tx = (width - tw) // 2
        ty = logo_y + logo_size + int(logo_size * 0.2)
        draw.text((tx, ty), text, fill=GOLD, font=font)
    img.info['dpi'] = (144, 144)
    img.info['Copyright'] = f"Copyright (c) 2026 KHAOS Engineering."
    return img

# ---------------------------------------------------------------------------
# 原子保存：先写临时文件，成功后再替换，失败则保留原文件
# ---------------------------------------------------------------------------
def atomic_save(img: Image.Image, filepath: Path, **kwargs) -> str:
    """原子化保存图像，返回校验和"""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(filepath.parent), suffix='.tmp')
        os.close(fd)
        os.chmod(tmp_path, 0o644)
        img.save(tmp_path, **kwargs)
        # 校验和
        sha = hashlib.sha256()
        with open(tmp_path, 'rb') as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        checksum = sha.hexdigest()
        # 原子替换
        os.replace(tmp_path, str(filepath))
        return checksum
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def save_and_record(img: Image.Image, name: str, out_dir: Path,
                    errors: List[str], manifest: Dict, **kwargs):
    filepath = out_dir / name
    try:
        checksum = atomic_save(img, filepath, **kwargs)
        logger.info(f"  ✓ {name}  ({img.size[0]}x{img.size[1]})  {checksum[:8]}...")
        manifest[name] = {"size": f"{img.size[0]}x{img.size[1]}", "sha256": checksum}
    except Exception as e:
        logger.error(f"  ✗ 无法保存 {name}: {e}")
        errors.append(name)
    finally:
        img.close()

# ---------------------------------------------------------------------------
# 主生成流程
# ---------------------------------------------------------------------------
def generate_all(output_dir: str, dry_run: bool = False,
                 skip_splash: bool = False, skip_icons: bool = False,
                 no_audit: bool = False):
    out = Path(output_dir).expanduser().resolve()
    if dry_run:
        logger.info("Dry-run 模式：仅列出计划，不生成文件。")
        plan = []
        if not skip_icons:
            plan += ["favicon.ico", "apple-touch-icon.png"] + \
                    [f"logo{s}.png" for s in [48,96,144,192,384,512]] + \
                    ["maskable-192.png", "maskable-512.png",
                     "shortcut-dashboard.png", "shortcut-config.png"]
        if not skip_splash:
            plan += ["screenshot-4k.png", "screenshot-mobile.png"] + \
                    [f"splash-{w}x{h}.png" for w,h in [(1125,2436),(828,1792),(1242,2688),(1290,2796)]]
        for f in plan:
            logger.info(f"  计划生成: {f}")
        return

    if skip_icons and skip_splash:
        logger.info("所有生成项均被跳过，无需生成。")
        return

    # 磁盘空间检查
    try:
        usage = shutil.disk_usage(str(out.parent))
        if usage.free < 100 * 1024 * 1024:
            logger.warning("磁盘可用空间不足 100MB，可能导致生成失败。")
    except Exception:
        logger.debug("无法检查磁盘空间，跳过。")

    out.mkdir(parents=True, exist_ok=True)
    if not out.is_dir():
        logger.critical(f"输出路径已存在但不是目录: {out}")
        sys.exit(1)
    if not os.access(str(out), os.W_OK):
        logger.critical(f"输出目录不可写: {out}")
        sys.exit(1)

    errors: List[str] = []
    manifest: Dict[str, Dict] = {}

    # 环境信息（可关闭）
    if not no_audit:
        try:
            user = os.getlogin()
        except Exception:
            user = "unknown"
        logger.info(f"主机: {platform.node()}, 用户: {user}, Python {sys.version.split()[0]}, Pillow {pillow_version}")

    total_planned = (0 if skip_icons else 13) + (0 if skip_splash else 6)
    logger.info(f"计划生成 {total_planned} 个文件。")

    if not skip_icons:
        # favicon.ico
        sizes = [16, 32, 48]
        fav_icons = [make_icon(s) for s in sizes]
        try:
            save_and_record(fav_icons[0], "favicon.ico", out, errors, manifest,
                            format='ICO', sizes=[(s,s) for s in sizes])
            # 简单校验
            with open(str(out / "favicon.ico"), 'rb') as f:
                header = f.read(4)
                if header != b'\x00\x00\x01\x00':
                    logger.warning("favicon.ico 文件头异常，可能不可用。")
        except Exception as e:
            logger.error(f"生成 favicon.ico 失败: {e}")
            errors.append("favicon.ico")
        for icon in fav_icons:
            icon.close()

        # Apple Touch Icon
        try:
            save_and_record(make_icon(180), "apple-touch-icon.png", out, errors, manifest, optimize=True)
        except Exception as e:
            logger.error(f"生成 apple-touch-icon.png 失败: {e}")
            errors.append("apple-touch-icon.png")

        # Logo 系列
        for size in [48, 96, 144, 192, 384, 512]:
            try:
                save_and_record(make_icon(size), f"logo{size}.png", out, errors, manifest, optimize=True)
            except Exception as e:
                errors.append(f"logo{size}.png")

        # Maskable 图标
        for size in [192, 512]:
            try:
                save_and_record(make_icon(size, maskable=True), f"maskable-{size}.png", out, errors, manifest, optimize=True)
            except Exception as e:
                errors.append(f"maskable-{size}.png")

        # 快捷方式图标 (D=Dashboard, C=Config)
        try:
            save_and_record(make_shortcut_icon(96, "D"), "shortcut-dashboard.png", out, errors, manifest, optimize=True)
        except Exception as e:
            errors.append("shortcut-dashboard.png")
        try:
            save_and_record(make_shortcut_icon(96, "C"), "shortcut-config.png", out, errors, manifest, optimize=True)
        except Exception as e:
            errors.append("shortcut-config.png")

    if not skip_splash:
        # 截图
        try:
            save_and_record(make_splash(3840, 2160, logo_scale=0.15, font_size_ratio=0.025),
                            "screenshot-4k.png", out, errors, manifest, optimize=True)
        except MemoryError:
            logger.error("内存不足，无法生成 4K 截图。")
            errors.append("screenshot-4k.png")
        except Exception as e:
            logger.error(f"生成 screenshot-4k.png 失败: {e}")
            errors.append("screenshot-4k.png")
        try:
            save_and_record(make_splash(1170, 2532, logo_scale=0.25, font_size_ratio=0.035),
                            "screenshot-mobile.png", out, errors, manifest, optimize=True)
        except Exception as e:
            logger.error(f"生成 screenshot-mobile.png 失败: {e}")
            errors.append("screenshot-mobile.png")
        # 启动画面
        splash_configs = [(1125, 2436), (828, 1792), (1242, 2688), (1290, 2796)]
        for w, h in splash_configs:
            try:
                save_and_record(make_splash(w, h), f"splash-{w}x{h}.png", out, errors, manifest, optimize=True)
            except Exception as e:
                errors.append(f"splash-{w}x{h}.png")

    # 资源清单 (仅在非空时写入)
    if manifest:
        manifest_data = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "icons": manifest
        }
        manifest_path = out / "icon-manifest.json"
        try:
            manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding='utf-8')
            logger.info(f"  资源清单已生成: {manifest_path.name}")
        except Exception as e:
            logger.error(f"  无法生成清单: {e}")

        # 校验和文件
        checksum_path = out / "checksums.sha256"
        try:
            lines = ["# KHAOS Icon Checksums"]
            for name in sorted(manifest.keys()):
                lines.append(f"{manifest[name]['sha256']}  {name}")
            checksum_path.write_text("\n".join(lines) + "\n", encoding='utf-8')
            logger.info(f"  校验和文件已生成: {checksum_path.name}")
        except Exception as e:
            logger.error(f"  无法生成校验和: {e}")

    if errors:
        logger.error(f"生成完成，但有 {len(errors)} 个文件失败: {', '.join(errors[:10])}")
        sys.exit(2)
    else:
        logger.info("所有品牌资源生成完毕，符合华尔街机构级标准。")

# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=f"KHAOS 品牌视觉资源生成器 v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("退出码:\n  0 - 成功\n  1 - 参数错误或依赖缺失\n"
                "  2 - 部分文件生成失败")
    )
    parser.add_argument("--output-dir", default="./public/icons",
                        help="输出目录 (默认: ./public/icons)")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    parser.add_argument("--dry-run", action="store_true", help="只列出计划生成的文件，不实际生成")
    parser.add_argument("--skip-splash", action="store_true", help="跳过启动画面与截图生成")
    parser.add_argument("--skip-icons", action="store_true", help="跳过所有图标生成")
    parser.add_argument("--no-audit", action="store_true", help="不在日志中记录主机与用户信息")
    parser.add_argument("--log-file", help="将日志同时输出到文件")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    # 日志级别与文件
    if args.quiet:
        logger.setLevel(logging.ERROR)
    if args.log_file:
        fh = logging.FileHandler(args.log_file, encoding='utf-8')
        fh.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        logger.addHandler(fh)

    # Python 版本检查
    if sys.version_info < (3, 8):
        logger.critical("需要 Python 3.8 或更高版本。")
        sys.exit(1)

    start = time.time()
    logger.info(f"KHAOS 品牌资源生成器 v{VERSION} 启动")
    generate_all(args.output_dir, dry_run=args.dry_run,
                 skip_splash=args.skip_splash, skip_icons=args.skip_icons,
                 no_audit=args.no_audit)
    elapsed = time.time() - start
    logger.info(f"总耗时: {elapsed:.2f} 秒")

if __name__ == "__main__":
    main()
