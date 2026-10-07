#!/usr/bin/env python3
"""Compile frozen and reorganized sources in scratch space; compare every page.

Delivery main.pdf files are never overwritten. Run with the scratch venv that
contains PyMuPDF. A passed report also records the exact delivered PDF hash.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pymupdf as fitz
import semantic_fragments as sf

ROOT = sf.ROOT
WORK = ROOT / 'work/semantic_fragments'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def tree_digest(source):
    result=hashlib.sha256()
    for path in [source / 'main.tex', *sorted((source / 'chapters').rglob('*.tex'))]:
        result.update(path.relative_to(source).as_posix().encode())
        result.update(b'\0'); result.update(path.read_bytes()); result.update(b'\0')
    result.update((WORK / 'fontconfig.conf').read_bytes())
    return result.hexdigest()

def normalized_links(page):
    return [{k: (list(v) if isinstance(v, (fitz.Rect, fitz.Point)) else v)
             for k, v in item.items() if k not in {'xref', 'id'}}
            for item in page.get_links()]

def exact_read(stream, size):
    chunks=[]
    while size:
        chunk=stream.read(size)
        if not chunk: raise ValueError('truncated Poppler page raster')
        chunks.append(chunk);size-=len(chunk)
    return b''.join(chunks)

def ppm_page(stream):
    magic=stream.readline()
    if not magic:return None
    if magic.strip()!=b'P6':raise ValueError('unexpected Poppler raster format')
    dimensions=stream.readline()
    while dimensions.startswith(b'#'):dimensions=stream.readline()
    width,height=map(int,dimensions.split())
    if stream.readline().strip()!=b'255':raise ValueError('unexpected raster channel depth')
    return width,height,exact_read(stream,width*height*3)

def compare_poppler(before,after,count):
    """Each PDF has a separate renderer process and private font/image caches."""
    raster=hashlib.sha256();processes=[];logs=[]
    try:
        for path in [before,after]:
            log=Path(path).with_name('render-96.log').open('w');logs.append(log)
            processes.append(subprocess.Popen(['pdftoppm','-r','96',str(path)],
                                               stdout=subprocess.PIPE,stderr=log))
        for index in range(count):
            old,new=(ppm_page(process.stdout) for process in processes)
            if old is None or new is None:raise ValueError('raster page count is too small')
            if old!=new:raise ValueError(f'page pixels differ at 96dpi (Poppler): page {index+1}')
            raster.update(old[2])
        if any(ppm_page(process.stdout) is not None for process in processes):
            raise ValueError('raster page count is too large')
        for process in processes:
            if process.wait():raise ValueError('Poppler rendering failed; inspect render-96.log')
        return raster.hexdigest()
    finally:
        for process in processes:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            if process.stdout:process.stdout.close()
        for log in logs:log.close()

def compare(before, after):
    a, b = fitz.open(before), fitz.open(after)
    if len(a) != len(b):
        raise ValueError(f'page counts differ: {len(a)} != {len(b)}')
    if a.get_toc() != b.get_toc():
        raise ValueError('PDF bookmarks differ')
    for index, (old, new) in enumerate(zip(a, b)):
        if old.rect != new.rect or old.rotation != new.rotation:
            raise ValueError(f'page dimensions differ: {index+1}')
        if old.get_text('rawdict') != new.get_text('rawdict'):
            raise ValueError(f'text/glyph positions differ: page {index+1}')
        if normalized_links(old) != normalized_links(new):
            raise ValueError(f'link targets/rectangles differ: page {index+1}')
    count = len(a)
    a.close(); b.close()
    raster=compare_poppler(before,after,count)
    return {'pages': count, 'all_page_pixels_identical_96dpi': True,
            'all_text_glyph_positions_identical': True,
            'all_page_dimensions_identical': True,
            'bookmarks_and_links_identical': True,
            'page_raster_sha256': raster,'renderer':'Poppler pdftoppm',
            'pdf_inspection_in_isolated_process':True}

def compare_isolated(before,after):
    # PyMuPDF does not support concurrent threads. Its complete document work
    # runs on one child process's main thread, even when compilations overlap.
    code=('import sys,json;sys.path.insert(0,sys.argv[1]);'
          'from fragment_pdf_gate import compare;'
          'print(json.dumps(compare(sys.argv[2],sys.argv[3])))')
    process=subprocess.run([sys.executable,'-c',code,str(Path(__file__).parent),str(before),str(after)],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if process.returncode:
        raise ValueError(process.stderr.strip()[-1800:])
    return json.loads(process.stdout)

def compile_tree(order, language, variant, source, book, timeout):
    volume = sf.get_volume(order)
    folder = WORK / 'pdf-gate' / volume['job'] / language / variant
    src, build = folder / 'source', folder / 'build'
    src.mkdir(parents=True, exist_ok=True); build.mkdir(exist_ok=True)
    fingerprint=tree_digest(source)
    cache=folder / 'compiled.json'
    pdf=build / 'main.pdf'
    if cache.exists() and pdf.exists():
        cached=sf.load_json(cache)
        if cached.get('tree_sha256') == fingerprint and cached.get('pdf_sha256') == digest(pdf):
            print(f'[info] retaining compiled PDF {order:02d}/{language}/{variant}',flush=True)
            return pdf
    # Use separate clean sources, so latexmk cannot touch delivery files or snapshots.
    for name in ('main.tex', 'chapters'):
        target = src / name
        if target.is_dir(): shutil.rmtree(target)
        elif target.exists(): target.unlink()
        if name == 'chapters': shutil.copytree(source / name, target)
        else: shutil.copyfile(source / name, target)
    for asset in book.iterdir():
        if asset.name in {'main.tex', 'chapters'} or asset.name.startswith('main.'):
            continue
        target = src / asset.name
        if not target.exists(): target.symlink_to(asset)
    env = os.environ.copy()
    env.update(FONTCONFIG_FILE=str(WORK / 'fontconfig.conf'),
               TEXINPUTS=str(WORK / 'texmf/tex')+'//:',
               OSFONTDIR=str(WORK / 'texmf/fonts')+'//:',
               SOURCE_DATE_EPOCH='1788000000', FORCE_SOURCE_DATE='1', TZ='UTC')
    command = ['latexmk', '-g', '-xelatex', '-interaction=nonstopmode',
               '-halt-on-error', '-outdir='+str(build), 'main.tex']
    print(f'[state] PDF compile {order:02d}/{language}/{variant}', flush=True)
    with (folder / 'compile.log').open('w') as log:
        process = subprocess.run(command, cwd=src, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=timeout)
    if process.returncode:
        raise ValueError(f'compile failed: {folder / "compile.log"}')
    sf.write_json(cache,{'tree_sha256':fingerprint,'pdf_sha256':digest(pdf)})
    return pdf

def gate_one(order, language, timeout, force):
    volume = sf.get_volume(order)
    directory = sf.task_dir(volume)
    book = sf.source_dir(volume, language)
    report_path = WORK / 'pdf-gate' / volume['job'] / language / 'verification.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    old = sf.load_source(directory / 'original' / language)
    current = sf.load_source(book)
    expected = sf.load_json(directory / 'plan-input.json')['source_hashes'][language]
    if old['expanded'] != current['expanded']:
        raise ValueError(f'expanded source differs before PDF gate: {order}/{language}')
    if digest(book / 'main.pdf') != expected['pdf_sha256']:
        raise ValueError(f'delivered PDF bytes differ: {order}/{language}')
    source_hash = sf.sha(current['expanded'])
    assembly_hash = tree_digest(book)
    if not force and report_path.exists():
        previous = sf.load_json(report_path)
        if (previous.get('ok') and previous.get('renderer')=='Poppler pdftoppm'
                and previous.get('pdf_inspection_in_isolated_process')
                and previous.get('source_expanded_sha256') == source_hash
                and previous.get('delivery_pdf_sha256') == expected['pdf_sha256']):
            if previous.get('assembly_tree_sha256') == assembly_hash:
                print(f'[info] retaining verified PDF gate {order:02d}/{language}', flush=True)
                return previous
    started = time.time()
    baseline = compile_tree(order, language, 'before', directory / 'original' / language, book, timeout)
    candidate = compile_tree(order, language, 'after', book, book, timeout)
    evidence = compare_isolated(baseline, candidate)
    result = {'schema': 'semantic-fragments-pdf-gate/v1', 'ok': True,
              'order': order, 'language': language, 'volume': volume['volume'],
              'source_expanded_sha256': source_hash,
              'assembly_tree_sha256': assembly_hash,
              'delivery_pdf_sha256': expected['pdf_sha256'],
              'delivery_pdf_bytes_unchanged': True,
              'before_compile_pdf_sha256': digest(baseline),
              'after_compile_pdf_sha256': digest(candidate),
              'elapsed_seconds': round(time.time()-started, 1), **evidence}
    sf.write_json(report_path, result)
    print(f'[state] PDF gate PASSED {order:02d}/{language}: {evidence["pages"]} pages', flush=True)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--order', type=int, action='append')
    p.add_argument('--language', choices=['en','zh'], action='append')
    p.add_argument('--workers', type=int, default=3)
    p.add_argument('--timeout', type=int, default=1800)
    p.add_argument('--force', action='store_true')
    args=p.parse_args()
    orders=args.order or [v['order'] for v in sf.load_json(sf.CATALOG)['volumes']]
    languages=args.language or ['en','zh']
    failures, results=[],[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks={pool.submit(gate_one, o,l,args.timeout,args.force):(o,l) for o in orders for l in languages}
        for task in concurrent.futures.as_completed(tasks):
            order,language=tasks[task]
            try: results.append(task.result())
            except Exception as error:
                failures.append({'order':order,'language':language,'error':str(error)})
                print(f'[error] PDF gate {order}/{language}: {error}',flush=True)
    sf.write_json(WORK / 'pdf-gate' / 'summary.json',
                  {'ok':not failures,'verified':results,'failures':failures})
    return 1 if failures else 0

if __name__=='__main__': sys.exit(main())
