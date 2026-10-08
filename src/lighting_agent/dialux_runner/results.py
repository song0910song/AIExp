"""Strict parser for the accepted DIALux 5.14 Chinese room-summary PDF.

This is a versioned PDF adapter, not a generic DIALux result API. Missing or
ambiguous fields fail closed. Software preset targets are never design rules.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import subprocess
import unicodedata

from .artifacts import RunError, sha256

PARSER_VERSION = 'evo-5.14-zh-room-summary-v1'
NUMBER = r'(\d+(?:[.,]\d+)?)'


def normalise(value: str) -> str:
    return unicodedata.normalize('NFKC', value).replace('\r', '')


def compact(value: str) -> str:
    return re.sub(r'\s+', ' ', normalise(value)).strip()


def extract_pdf(pdf: Path, text_path: Path, pdftotext: str = 'pdftotext', *, timeout: float = 30) -> str:
    if not pdf.is_file() or pdf.read_bytes()[:5] != b'%PDF-':
        raise RunError('Export is not a PDF')
    try:
        result = subprocess.run([pdftotext, '-layout', '-enc', 'UTF-8', str(pdf), str(text_path)],
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunError(f'PDF extraction failed: {error}') from error
    if result.returncode or not text_path.is_file():
        raise RunError(f'PDF extraction failed: {result.stderr.decode("utf-8", errors="replace")[:400]}')
    return text_path.read_text(encoding='utf-8')


def parse_summary(text: str, *, expected: dict, pdf_sha256: str, source_name: str) -> dict:
    pages = [normalise(p) for p in text.split('\f') if p.strip()]
    if len(pages) != 2:
        raise RunError(f'Room-summary profile requires exactly two pages, got {len(pages)}')
    header = compact(f'{expected["building"]} · {expected["floor"]} · {expected["room"]} ({expected["scene"]})')
    for number, page in enumerate(pages, 1):
        headers = [compact(line) for line in page.splitlines() if ' · ' in line]
        if headers != [header]:
            raise RunError(f'PDF page {number} has a missing, ambiguous or different room/scene header')

    def metric(pattern: str, page_index: int, unit: str, scope: str, *, body: str | None = None) -> dict:
        region = pages[page_index] if body is None else body
        matches = list(re.finditer(pattern, region, flags=re.MULTILINE))
        if len(matches) != 1:
            raise RunError(f'Missing or ambiguous PDF field: {scope} ({len(matches)} matches)')
        match = matches[0]
        value = float(match.group(1).replace(',', '.'))
        if not math.isfinite(value):
            raise RunError(f'Non-finite PDF value: {scope}')
        reported_text = match.group(1).replace(',', '.')
        decimals = len(reported_text.split('.', 1)[1]) if '.' in reported_text else 0
        return {'value': value, 'unit': unit, 'scope': scope,
                'reported_decimals': decimals,
                'source': {'file': source_name, 'sha256': pdf_sha256, 'page': page_index + 1,
                           'quote': match.group(0).strip(), 'parser': PARSER_VERSION}}

    conditions = {
        'floor_area_m2': metric(r'^\s*地面\s+' + NUMBER + r'\s+m2\s*$', 0, 'm2', 'room'),
        'workplane_height_m': metric(r'^.*高度\s+工作面\s+' + NUMBER + r'\s+m\s*$', 0, 'm', 'workplane height'),
        'edge_margin_m': metric(r'^.*边缘区\s+工作面\s+' + NUMBER + r'\s+m\s*$', 0, 'm', 'workplane margin'),
        'maintenance_factor': metric(r'^\s*维护系数\s+' + NUMBER + r'\s+.*$', 0, '1', 'maintenance factor'),
        'mounting_height_m': metric(r'^.*安装高度\s+' + NUMBER + r'\s+m\s*$', 0, 'm', 'mounting height'),
        'ceiling_reflectance': metric(r'^.*天花板:\s*' + NUMBER + r'\s*%.*$', 0, '%', 'ceiling reflectance'),
        'wall_reflectance': metric(r'^\s*墙壁:\s*' + NUMBER + r'\s*%.*$', 0, '%', 'wall reflectance'),
        'floor_reflectance': metric(r'^\s*地板:\s*' + NUMBER + r'\s*%.*$', 0, '%', 'floor reflectance'),
    }
    for key, observed in conditions.items():
        target = expected[key] * (100 if observed['unit'] == '%' else 1)
        # DIALux room summaries round conditions (notably large CAD areas) to
        # the displayed precision.  Compare against half a report unit while
        # retaining the exact job value in job.json for provenance.
        tolerance = max(.0001, .5 * 10 ** -observed.get('reported_decimals', 4))
        if not math.isclose(observed['value'], target, rel_tol=0, abs_tol=tolerance):
            raise RunError(f'PDF calculation condition differs from job: {key}={observed["value"]}, expected {target}')
    page = pages[1]
    # Section boundaries prevent confusing workplane LPD, room LPD and the
    # adjacent values normalised to 100 lx. Each required section is unique.
    for label in ('工作面', '眩光评估', '空间', '灯具列表'):
        if len(re.findall(r'^\s*' + label, page, re.MULTILINE)) != 1:
            raise RunError(f'Ambiguous result section: {label}')
    workplane = page[re.search(r'^\s*工作面', page, re.MULTILINE).start():
                     re.search(r'^\s*眩光评估', page, re.MULTILINE).start()]
    room = page[re.search(r'^\s*空间', page, re.MULTILINE).start():
                re.search(r'^\s*\(1\)', page, re.MULTILINE).start()] if re.search(r'^\s*\(1\)', page, re.MULTILINE) else ''
    configured_index = expected['surface_index']
    if configured_index is None:
        # Multiple imported rooms do not get deterministic WP numbers. Both
        # result rows must independently identify the same single real surface.
        observed = re.findall(r'^\s*工作面\s+Ē直角\s+\d+(?:[.,]\d+)?\s+lx\s+≥\s*\d+(?:[.,]\d+)?\s+lx\s+(WP[1-9]\d*)\s*$',
                              workplane, re.MULTILINE)
        observed_uniformity = re.findall(r'^\s*Uo\s*\(g1\)\s+\d+(?:[.,]\d+)?\s+≥\s*\d+(?:[.,]\d+)?\s+(WP[1-9]\d*)\s*$',
                                         workplane, re.MULTILINE)
        if len(observed) != 1 or observed_uniformity != observed:
            raise RunError('PDF workplane index is missing, ambiguous or inconsistent between metrics')
        configured_index = observed[0]
    index = re.escape(configured_index)
    metrics = {
        'average_illuminance_lx': metric(r'^\s*工作面\s+Ē直角\s+' + NUMBER + r'\s+lx\s+≥\s*\d+(?:[.,]\d+)?\s+lx\s+' + index + r'\s*$',
                                         1, 'lx', expected['surface'], body=workplane),
        'uniformity_u0': metric(r'^\s*Uo\s*\(g1\)\s+' + NUMBER + r'\s+≥\s*\d+(?:[.,]\d+)?\s+' + index + r'\s*$',
                                1, '1', expected['surface'], body=workplane),
        'workplane_lpd_w_m2': metric(r'^\s*照明功率密度\s+' + NUMBER + r'\s+W/m2\s+[^\n]*$',
                                    1, 'W/m2', expected['surface'], body=workplane),
        'room_lpd_w_m2': metric(r'^\s*空间\s+照明功率密度\s+' + NUMBER + r'\s+W/m2\s+[^\n]*$',
                                1, 'W/m2', expected['room'], body=room),
    }
    if not 0 <= metrics['uniformity_u0']['value'] <= 1:
        raise RunError('Uniformity lies outside [0, 1]')
    glare = metric(r'^\s*眩光评估\(1\)\s+RUG,\s*max\s+' + NUMBER + r'\s+≤\s*\d+\s*$', 1, '1', 'report rectangular-room glare')
    glare_conditions = re.findall(r'^\(1\)\s+基于\s+.*?SHR\s*$', page, re.MULTILINE)
    if len(glare_conditions) != 1:
        raise RunError('Missing or ambiguous glare method footnote')
    glare.update(label='RUG,max', method='dialux_rectangular_room_report',
                 method_note=glare_conditions[0].strip(), verified_observer_calculation=False)
    return {
        'schema_version': 1, 'parser': PARSER_VERSION, 'dialux_version': '5.14.0.3',
        'room': expected['room'], 'scene': expected['scene'], 'surface': expected['surface'],
        'surface_index': configured_index, 'conditions': conditions,
        'metrics': metrics, 'glare': glare,
        'compliance': {'status': 'not_evaluated', 'reason': 'DIALux preset targets are not confirmed project rules'},
        'limitations': ['Fixed Chinese two-page room-summary PDF profile; XML/CSV not validated.',
                        'No verified observer glare, CRI or CCT result is available from this report.'],
    }


def parse_pdf(pdf: Path, text_path: Path, expected: dict, *, pdftotext: str = 'pdftotext') -> dict:
    text = extract_pdf(pdf, text_path, pdftotext)
    return parse_summary(text, expected=expected, pdf_sha256=sha256(pdf), source_name=pdf.name)
