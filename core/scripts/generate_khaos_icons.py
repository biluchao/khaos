#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本名称: generate_khaos_icons.py
核心职责: 生成 KHAOS 量化交易系统所需的 favicon.ico 及 apple-touch-icon.png
         符合华尔街机构级品牌标准，支持多尺寸、暗黑模式、透明背景。
依赖: Pillow >= 9.0.0
输出: favicon.ico (16/32/48px), apple-touch-icon.png (180x180px)
用法: python generate_khaos_icons.py --output-dir ../frontend/public/
作者: KHAOS Design Team
创建日期: 2026-07-09
修改记录: 初始版本
"""
import argparse
import math
from PIL import Image, ImageDraw

# KHAOS 品牌颜色定义
GOLD = (232, 193, 112, 255)       # #e8c170
DARK_BG = (10, 14, 23, 255)       # #0a0e17
TRANSPARENT = (0, 0, 0, 0)

def draw_khaos_shield(draw, size, center, radius):
    """绘制一个简洁的盾牌形状，代表安全与防护"""
    w, h = size, size
    # 盾牌轮廓：底部尖角，顶部平缓
    shield_points = [
        (center - radius * 0.9, center - radius * 1.1),  # 左上
        (center + radius * 0.9, center - radius * 1.1),  # 右上
        (center + radius * 0.7, center + radius * 0.3),  # 右侧中
        (center, center + radius * 1.2),                 # 底部尖端
        (center - radius * 0.7, center + radius * 0.3),  # 左侧中
    ]
    draw.polygon(shield_points, fill=GOLD, outline=DARK_BG)

def draw_khaos_k(draw, size, center, radius):
    """在盾牌中央绘制字母 'K' 的抽象几何图形"""
    # 使用简单的三条线构成 K
    k_color = DARK_BG
    lw = max(1, int(radius * 0.3))
    # 垂直线
    draw.line([(center - radius * 0.35, center - radius * 0.7),
               (center - radius * 0.35, center + radius * 0.7)],
              fill=k_color, width=lw)
    # 上斜线
    draw.line([(center - radius * 0.35, center - radius * 0.1),
               (center + radius * 0.55, center - radius * 0.75)],
              fill=k_color, width=lw)
    # 下斜线
    draw.line([(center - radius * 0.35, center + radius * 0.1),
               (center + radius * 0.55, center + radius * 0.65)],
              fill=k_color, width=lw)

def create_khaos_icon(size):
    """生成一个尺寸为 size x size 的 KHAOS 图标，返回 PIL Image 对象"""
    img = Image.new('RGBA', (size, size), TRANSPARENT)
    draw = ImageDraw.Draw(img)
    # 绘制深色圆形背景
    margin = size * 0.1
    draw.ellipse([margin, margin, size - margin, size - margin],
                 fill=DARK_BG)
    # 盾牌与 K 字母
    center = size / 2
    radius = size * 0.38
    draw_khaos_shield(draw, size, center, radius)
    draw_khaos_k(draw, size, center, radius)
    return img

def generate_icons(output_dir):
    """生成所有尺寸的图标并保存"""
    import os
    os.makedirs(output_dir, exist_ok=True)

    # 生成不同尺寸的图标
    sizes = [16, 32, 48]
    images = []
    for s in sizes:
        icon = create_khaos_icon(s)
        images.append(icon)

    # 保存 ICO 文件（包含所有尺寸）
    ico_path = os.path.join(output_dir, 'favicon.ico')
    images[0].save(ico_path, format='ICO', sizes=[(s, s) for s in sizes])
    print(f'✓ favicon.ico 已生成 (包含 {sizes} 尺寸) -> {ico_path}')

    # 生成 Apple Touch Icon (180x180)
    apple_icon = create_khaos_icon(180)
    apple_path = os.path.join(output_dir, 'apple-touch-icon.png')
    apple_icon.save(apple_path, format='PNG')
    print(f'✓ apple-touch-icon.png 已生成 -> {apple_path}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成 KHAOS 品牌图标')
    parser.add_argument('--output-dir', default='./', help='输出目录')
    args = parser.parse_args()
    generate_icons(args.output_dir)
