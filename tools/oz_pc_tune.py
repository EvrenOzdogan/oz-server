#!/usr/bin/env python3
"""Oz Server: optional PC performance setup for Project Zomboid (player side, Windows).

What it does, and ONLY this:
  1. READS your PC and prints a table: each setting, what it is now, what we recommend, and why.
  2. Asks you to type YES. Only then does it change the rows marked "change":
       Java heap      ProjectZomboid64.json: the -Xmx value only (other vmArgs are left as they are)
       GPU scheduling HKLM\\SYSTEM\\CurrentControlSet\\Control\\GraphicsDrivers  HwSchMode = 2
                      (needs an Administrator terminal and a reboot; skipped if not admin)
       GPU preference HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences  value named after
                      ProjectZomboid64.exe = GpuPreference=2 (High performance); other keys in that
                      value are kept
       Auto HDR       only with --auto-hdr-off: AutoHDREnable=2097 for the game (off). Optional.
  3. Backs up every file/registry value it changes into ~/.oz_pc_tune/backups/<time>/ (a copy of the
     json, a .reg export of each registry key, and a manifest) BEFORE writing. --undo restores them.

It does NOT touch anything else: no drivers, no NVIDIA settings (NVIDIA is instructions only), no
Steam settings (launch options are checked, never edited), no other game.
There is no flag that skips the YES question. --dry-run prints the table and changes nothing.

Why each setting (measured on our own PC, RTX 5070 Ti / 32 GB, modded Oz Server):
  Heap: the modded server's heap use peaked near 10.5 GB. Below about 11 GB the JVM thrashes in GC
        (freezes of many seconds). So: 32 GB RAM -> 12g, 24 GB -> 10g (tight), 16 GB -> 8g (expect
        stutter and long freezes in the city; 16 GB PCs will struggle). Under 16 GB: no change.
        A Steam "Verify integrity of game files" resets ProjectZomboid64.json; run this again after.
  GPU scheduling (HAGS): the GPU manages its own queue; less CPU stall on the render thread.
  GPU preference: forces the real GPU (not the integrated one) for the game.
  Auto HDR: the Windows 11 Auto HDR path costs frame time; irrelevant without an HDR display.
  NVIDIA profile (manual, printed under the table): max performance power mode + threaded optimization.
Viewpoint's own graphics settings are NOT done here: the optional oz-viewpoint-settings.properties
on the Oz site (or the OzFastBoot preset) handles those.

Close Project Zomboid first.
Memory tiers (picked from RAM and VRAM; only LOWERS your values): also edits Zomboid\viewpoint-live.properties
(memory.packMiB/shellMiB/floorMiB/cpuCacheMiB/nearMeshMiB/farMiB/minecraftMiB) plus -XX:SoftMaxHeapSize and
-XX:+UseStringDeduplication in ProjectZomboid64.json. Backed up and restored by --undo like the rest.

Usage:  python oz_pc_tune.py [--dry-run] [--auto-hdr-off] [--steam-dir DIR]
        python oz_pc_tune.py --undo [BACKUP_FOLDER]     (default: the newest backup)
"""
import argparse, ctypes, datetime, json, os, re, shutil, subprocess, sys

HOME = os.path.expanduser('~')
BACKUP_ROOT = os.path.join(HOME, '.oz_pc_tune', 'backups')
GFX_KEY = r'SYSTEM\CurrentControlSet\Control\GraphicsDrivers'
PREF_KEY = r'Software\Microsoft\DirectX\UserGpuPreferences'
NIP = 'oz_pz_profile.nip'

try:
    import winreg
except ImportError:  # not Windows
    winreg = None


# ---------- discovery ----------
def steam_libraries(steam_dir=None):
    cands = [steam_dir] if steam_dir else []
    if winreg:
        cands += [r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam']
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as k:
                cands.append(winreg.QueryValueEx(k, 'SteamPath')[0].replace('/', '\\'))
        except OSError:
            pass
    libs, roots = [], []
    for c in cands:
        if c and os.path.isdir(c):
            libs.append(c)
            roots.append(c)
            vdf = os.path.join(c, 'steamapps', 'libraryfolders.vdf')
            if os.path.isfile(vdf):
                t = open(vdf, encoding='utf-8', errors='replace').read()
                libs += [m.group(1).replace('\\\\', '\\') for m in re.finditer(r'"path"\s+"([^"]+)"', t)]
    seen, out = set(), []
    for l in libs:
        k = os.path.normcase(os.path.abspath(l))
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out, roots


def find_game(libs):
    for l in libs:
        d = os.path.join(l, 'steamapps', 'common', 'ProjectZomboid')
        if os.path.isfile(os.path.join(d, 'ProjectZomboid64.json')):
            return d
    return None


def total_ram_gb():
    class MS(ctypes.Structure):
        _fields_ = [('l', ctypes.c_ulong), ('ml', ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in
                    ('tp', 'ap', 'tpf', 'apf', 'tv', 'av', 'ae')]
    s = MS()
    s.l = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.tp / (1 << 30)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def gpu_names():
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
                            '(Get-CimInstance Win32_VideoController).Name -join "|"'],
                           capture_output=True, text=True, timeout=30)
        return [x.strip() for x in r.stdout.split('|') if x.strip()]
    except Exception:
        return []


def game_running():
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ProjectZomboid64.exe'],
                           capture_output=True, text=True, timeout=20)
        return 'ProjectZomboid64.exe' in r.stdout
    except Exception:
        return False


def reg_get(hive, key, name):
    try:
        with winreg.OpenKey(hive, key) as k:
            return winreg.QueryValueEx(k, name)  # (value, type)
    except OSError:
        return None


def kv_parse(s):
    return [p for p in (s or '').split(';') if p]


def kv_set(s, key, val):
    parts = [p for p in kv_parse(s) if not p.lower().startswith(key.lower() + '=')]
    parts.append(f'{key}={val}')
    return ';'.join(parts) + ';'


def kv_get(s, key):
    for p in kv_parse(s):
        if p.lower().startswith(key.lower() + '='):
            return p.split('=', 1)[1]
    return None


def steam_launch_options(roots):
    found = None
    for r in roots:
        ud = os.path.join(r, 'userdata')
        if not os.path.isdir(ud):
            continue
        for f in sorted(os.listdir(ud)):
            cfg = os.path.join(ud, f, 'config', 'localconfig.vdf')
            if os.path.isfile(cfg):
                t = open(cfg, encoding='utf-8', errors='replace').read()
                i = t.find('"108600"')
                if i >= 0:
                    m = re.search(r'"LaunchOptions"\s+"([^"]*)"', t[i:i + 4000])
                    if m and m.group(1):
                        return m.group(1)
                    found = ''
    return found


# ---------- memory tiers ----------
VP_FILE = os.path.join(HOME, 'Zomboid', 'viewpoint-live.properties')
# caps in MiB for the top tier (basis: owner's 32 GB / 16 GB VRAM PC, p302 memory_plan.md)
TOP_CAPS = {'memory.packMiB': 1280, 'memory.shellMiB': 2048, 'memory.floorMiB': 1536,
            'memory.cpuCacheMiB': 512, 'memory.nearMeshMiB': 896, 'memory.farMiB': 512}
MC_CAPS = {'high': 2048, 'mid': 1792, 'low': 1536}   # Photon pool is ~1.4 GB fixed; never cut below it
TIERS = {  # name: (scale, xmx cap GB or None, soft max GB)
    'high': (1.0, None, 6), 'mid': (0.6, 6, 5), 'low': (0.4, 5, 4)}


def pick_tier(ram_gb, vram_gb):
    if ram_gb < 13 or vram_gb <= 6.5:
        return 'low'
    if ram_gb < 30 or vram_gb < 11.5:
        return 'mid'
    return 'high'


def tier_caps(tier):
    sc = TIERS[tier][0]
    d = {k: int(round(v * sc / 16.0)) * 16 for k, v in TOP_CAPS.items()}
    d['memory.minecraftMiB'] = MC_CAPS[tier]
    return d


def vram_gb():
    """VRAM in GB: nvidia-smi, else WMI AdapterRAM (saturates at 4 GB), else assume 8."""
    try:
        r = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=20)
        v = [float(x) for x in r.stdout.split() if re.fullmatch(r'\d+(\.\d+)?', x)]
        if v:
            return max(v) / 1024, 'nvidia-smi'
    except Exception:
        pass
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command',
                            '(Get-CimInstance Win32_VideoController).AdapterRAM -join "|"'],
                           capture_output=True, text=True, timeout=30)
        v = [int(x) for x in r.stdout.strip().split('|') if x.strip().isdigit()]
        if v and 0 < max(v) < 4290000000:   # 32-bit field saturates near 4 GB: not trustworthy
            return max(v) / (1 << 30), 'WMI'
    except Exception:
        pass
    return 8.0, 'assumed'


def props_plan(txt, caps):
    """Return (new_text, rows[(key, now, target, lowered)]). Only lowers; missing keys are added."""
    lines = txt.split('\n') if txt else []
    rows = []
    for k, t in caps.items():
        idx = next((i for i, l in enumerate(lines) if re.match(r'\s*' + re.escape(k) + r'\s*[=:]', l)), None)
        cur = None
        if idx is not None:
            m = re.match(r'\s*[^=:]+[=:]\s*(\d+)\s*$', lines[idx])
            cur = int(m.group(1)) if m else None
            if cur is None:        # non-numeric value: leave it alone
                rows.append((k, lines[idx].split('=', 1)[-1].strip(), t, False))
                continue
        if cur is not None and cur <= t:
            rows.append((k, cur, t, False))
            continue
        if idx is None:
            while lines and lines[-1] == '':
                lines.pop()
            lines.append(f'{k}={t}')
        else:
            lines[idx] = f'{k}={t}'
        rows.append((k, cur, t, True))
    return '\n'.join(lines).rstrip('\n') + '\n', rows


def java_edit(txt, xmx_cap, soft):
    """Lower -Xmx to xmx_cap GB if above; add/lower SoftMaxHeapSize (<= Xmx); add UseStringDeduplication."""
    m = re.search(r'"-Xmx(\w+)"', txt)
    if not m:
        return None
    xmx = m.group(1)
    if xmx_cap is not None and (gb(xmx) or 0) > xmx_cap:
        xmx = f'{xmx_cap}g'
    soft = min(soft, int(gb(xmx) or soft))
    tok = f'"-Xmx{xmx}"'
    out = txt[:m.start()] + tok + txt[m.end():]
    ls = out.rfind('\n', 0, out.index(tok)) + 1
    pad = '\n' + re.match(r'[ \t]*', out[ls:]).group(0)
    add = ''
    ms = re.search(r'"-XX:SoftMaxHeapSize=(\w+)"', out)
    if ms:
        if (gb(ms.group(1)) or 0) > soft:
            out = out[:ms.start()] + f'"-XX:SoftMaxHeapSize={soft}g"' + out[ms.end():]
    else:
        add += f',{pad}"-XX:SoftMaxHeapSize={soft}g"'
    if '-XX:+UseStringDeduplication' not in out:
        add += f',{pad}"-XX:+UseStringDeduplication"'
    if add:
        e = out.index(tok) + len(tok)
        out = out[:e] + add + out[e:]
    return out


# ---------- plan ----------
def heap_reco(ram):
    if ram >= 30:
        return '12g', ''
    if ram >= 22:
        return '10g', 'tight: the modded server peaks near 10.5 GB of heap; close other programs'
    if ram >= 15:
        return '8g', 'WARNING: 16 GB PCs will struggle on the modded server (stutter, long freezes)'
    return None, 'under 16 GB RAM: the modded server is not realistic; no change suggested'


def gb(s):
    m = re.fullmatch(r'(\d+)([gGmMkK])', s or '')
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2).lower()
    return n if u == 'g' else n / 1024 if u == 'm' else n / (1 << 20)


def build_plan(a):
    rows, acts = [], []   # rows: (setting, now, recommended, action, why)
    ram = total_ram_gb()
    libs, roots = steam_libraries(a.steam_dir)
    game = find_game(libs)
    admin = is_admin()

    # 0 memory tier
    vram, vsrc = vram_gb()
    tier = pick_tier(ram, vram)
    _, xcap, soft = TIERS[tier]
    rows.append(('Memory tier', f'{ram:.0f} GB RAM, {vram:.0f} GB VRAM ({vsrc})', tier, 'info',
                 'chosen from RAM and VRAM; only ever lowers your own settings'))
    if tier == 'low':
        rows.append(('HD texture packs', 'PZHDWalls / PZHDTerrain', 'optional', 'info',
                     'WARNING: the HD texture packs use ~2.8 GB of VRAM and are optional; unsubscribe them if the game stutters or crashes'))

    # 1 heap (+ soft max + string dedup)
    reco, note = heap_reco(ram)
    if not game:
        rows.append(('Java heap (-Xmx)', 'game not found', '-', 'skip', 'Project Zomboid not found in any Steam library'))
    else:
        jpath = os.path.join(game, 'ProjectZomboid64.json')
        txt = open(jpath, encoding='utf-8').read()
        m = re.search(r'"-Xmx(\w+)"', txt)
        why = f'{ram:.0f} GB RAM. {note}'.strip()
        if not m:
            rows.append(('Java heap (-Xmx)', 'no -Xmx line', '-', 'skip', 'no -Xmx in the json; not editing an unknown layout'))
        else:
            cur = m.group(1)
            base = txt
            newx = cur
            if reco and gb(cur) is not None and gb(cur) < gb(reco):   # existing logic: raise to the RAM recommendation
                newx = reco
            if xcap and (gb(newx) or 0) > xcap:                         # tier cap: lower only
                newx = f'{xcap}g'
            if newx != cur:
                base = txt[:m.start()] + f'"-Xmx{newx}"' + txt[m.end():]
                rows.append(('Java heap (-Xmx)', '-Xmx' + cur, '-Xmx' + newx, 'change', why))
            else:
                rows.append(('Java heap (-Xmx)', '-Xmx' + cur, '-Xmx' + cur, 'already set',
                             why + ' (kept)' if reco or xcap else why))
            out = java_edit(base, xcap, soft)
            ms = re.search(r'SoftMaxHeapSize=(\w+)', txt)
            smt = min(soft, int(gb(newx) or soft))
            sch = (not ms) or (gb(ms.group(1)) or 0) > smt
            rows.append(('Java SoftMaxHeapSize', ms.group(1) if ms else 'not set', f'{smt}g',
                         'change' if sch else 'already set', 'GC aims below this and returns RAM to Windows; the hard cap stays -Xmx'))
            sd = '-XX:+UseStringDeduplication' in txt
            rows.append(('Java StringDeduplication', 'on' if sd else 'off', 'on', 'already set' if sd else 'change',
                         'saves 0.1-0.3 GB of live strings (bundled Java 25 supports both flags)'))
            if out != txt:
                acts.append(dict(kind='file', path=jpath, new=out))

    # 1b Viewpoint memory caps
    old = open(VP_FILE, encoding='utf-8', errors='replace').read() if os.path.isfile(VP_FILE) else ''
    ntxt, prow = props_plan(old, tier_caps(tier))
    for k, cur, t, low in prow:
        rows.append((k, 'not set (default)' if cur is None else cur, t, 'change' if low else 'already set',
                     'cap on a Viewpoint pool (MiB); lowered only; applied at next game start'))
    if any(r[3] for r in prow):
        acts.append(dict(kind='file', path=VP_FILE, new=ntxt, create=not os.path.isfile(VP_FILE)))

    # 2 windows
    if winreg:
        hv = reg_get(winreg.HKEY_LOCAL_MACHINE, GFX_KEY, 'HwSchMode')
        v = hv[0] if hv else None
        cur = {None: 'not set (Windows default)', 1: '1 (off)', 2: '2 (on)'}.get(v, str(v))
        why = 'less CPU stall on the render thread; needs a reboot; supported GPUs only'
        if v == 2:
            rows.append(('Hardware GPU scheduling', cur, '2 (on)', 'already set', why))
        elif not admin:
            rows.append(('Hardware GPU scheduling', cur, '2 (on)', 'skip (not admin)',
                         why + '. Re-run from an Administrator terminal, or use Settings > System > Display > Graphics > Default graphics settings'))
        else:
            rows.append(('Hardware GPU scheduling', cur, '2 (on)', 'change', why))
            acts.append(dict(kind='reg', hive='HKLM', key=GFX_KEY, name='HwSchMode', new=2, rtype=winreg.REG_DWORD))

        if game:
            exe = os.path.join(game, 'ProjectZomboid64.exe')
            pv = reg_get(winreg.HKEY_CURRENT_USER, PREF_KEY, exe)
            curs = pv[0] if pv else ''
            gp = kv_get(curs, 'GpuPreference')
            why = 'use the real GPU, not the integrated one'
            new = curs
            if gp == '2':
                rows.append(('Graphics preference (game)', curs, 'GpuPreference=2 (High performance)', 'already set', why))
            else:
                rows.append(('Graphics preference (game)', curs or 'not set', 'GpuPreference=2 (High performance)', 'change', why))
                new = kv_set(new, 'GpuPreference', '2')
            ah = kv_get(curs, 'AutoHDREnable')
            why = 'Auto HDR costs frame time; only matters with an HDR display. Optional (--auto-hdr-off)'
            if ah == '2097':
                rows.append(('Auto HDR (game)', ah, 'off (AutoHDREnable=2097)', 'already set', why))
            elif a.auto_hdr_off:
                rows.append(('Auto HDR (game)', ah or 'not set', 'off (AutoHDREnable=2097)', 'change', why))
                new = kv_set(new, 'AutoHDREnable', '2097')
            else:
                rows.append(('Auto HDR (game)', ah or 'not set', 'off (AutoHDREnable=2097)', 'optional, not selected', why))
            if new != curs:
                acts.append(dict(kind='reg', hive='HKCU', key=PREF_KEY, name=exe, new=new, rtype=winreg.REG_SZ))

    # 3 nvidia
    gpus = gpu_names()
    nv = [g for g in gpus if 'nvidia' in g.lower()]
    if nv:
        rows.append(('NVIDIA profile', nv[0], 'import ' + NIP, 'manual', 'power = prefer max performance, threaded optimization = on; steps printed below'))
    else:
        rows.append(('NVIDIA profile', ', '.join(gpus) or 'unknown GPU', '-', 'skip', 'not an NVIDIA GPU'))

    # 4/5 info
    rows.append(('Viewpoint graphics settings', '-', 'oz-viewpoint-settings.properties', 'not done here', 'handled by the optional settings file / OzFastBoot preset, not by this script'))
    lo = steam_launch_options(roots)
    in_json = bool(game) and '-agentlib:zbNative' in open(os.path.join(game, 'ProjectZomboid64.json'), encoding='utf-8').read()
    if lo and '-agentlib:zbNative' in lo:
        rows.append(('ZombieBuddy hook', lo, '-agentlib:zbNative --', 'ok (check only)', 'set in Steam launch options; never edited by this script'))
    elif in_json:
        rows.append(('ZombieBuddy hook', 'in ProjectZomboid64.json', '-agentlib:zbNative', 'ok (check only)',
                     'the ZombieBuddy installer put it in the json vmArgs (Steam launch options: ' + (lo or 'empty') + '); never edited here'))
    else:
        rows.append(('ZombieBuddy hook', lo or 'empty', '-agentlib:zbNative --', 'MISSING (check only)',
                     'ZombieBuddy needs it: run its installer (choose Both) or put -agentlib:zbNative -- in Steam > Properties > Launch options'))
    return rows, acts, game


def print_table(rows):
    heads = ('Setting', 'Now', 'Recommended', 'Action')

    def cut(s, n):
        s = str(s)
        return s if len(s) <= n else s[:n - 1] + '~'
    w = [min(max(len(str(r[i])) for r in rows + [heads]), 44) for i in range(4)]
    line = lambda r: '  '.join(cut(r[i], w[i]).ljust(w[i]) for i in range(4))
    print(line(heads))
    print('  '.join('-' * x for x in w))
    for r in rows:
        print(line(r))
    print('\nWhy:')
    for r in rows:
        print(f'  - {r[0]}: {r[4]}')


# ---------- apply / undo ----------
HIVES = {'HKLM': 'HKEY_LOCAL_MACHINE', 'HKCU': 'HKEY_CURRENT_USER'}


def hive_obj(n):
    return getattr(winreg, HIVES[n])


def apply(acts):
    bdir = os.path.join(BACKUP_ROOT, datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(bdir)
    man = []
    for i, a in enumerate(acts):  # back up everything first
        if a['kind'] == 'file' and a.get('create'):
            man.append(dict(kind='file', path=a['path'], backup=None))
        elif a['kind'] == 'file':
            dst = os.path.join(bdir, f'{i}_' + os.path.basename(a['path']))
            shutil.copy2(a['path'], dst)
            man.append(dict(kind='file', path=a['path'], backup=dst))
        else:
            old = reg_get(hive_obj(a['hive']), a['key'], a['name'])
            reg_file = os.path.join(bdir, f'{i}_{a["hive"]}.reg')
            subprocess.run(['reg', 'export', f'{HIVES[a["hive"]]}\\{a["key"]}', reg_file, '/y'], capture_output=True)
            man.append(dict(kind='reg', hive=a['hive'], key=a['key'], name=a['name'],
                            old=None if old is None else old[0], old_type=None if old is None else old[1],
                            reg_export=reg_file))
    json.dump(man, open(os.path.join(bdir, 'manifest.json'), 'w'), indent=1)
    for a in acts:
        if a['kind'] == 'file':
            os.makedirs(os.path.dirname(a['path']), exist_ok=True)
            with open(a['path'], 'w', encoding='utf-8', newline='') as f:
                f.write(a['new'])
        else:
            with winreg.CreateKeyEx(hive_obj(a['hive']), a['key'], 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, a['name'], 0, a['rtype'], a['new'])
    print(f'\nDone. Backup: {bdir}\nUndo with:  python oz_pc_tune.py --undo')


def undo(path):
    if not path:
        if not os.path.isdir(BACKUP_ROOT) or not os.listdir(BACKUP_ROOT):
            sys.exit('No backups found in ' + BACKUP_ROOT)
        path = os.path.join(BACKUP_ROOT, sorted(os.listdir(BACKUP_ROOT))[-1])
    man = json.load(open(os.path.join(path, 'manifest.json')))
    print('Restoring from', path)
    if game_running():
        sys.exit('Close Project Zomboid first.')
    for m in man:
        if m['kind'] == 'file' and m['backup'] is None:
            if os.path.isfile(m['path']):
                os.remove(m['path'])
            print('  removed (did not exist before)', m['path'])
        elif m['kind'] == 'file':
            shutil.copy2(m['backup'], m['path'])
            print('  restored', m['path'])
        else:
            if m['hive'] == 'HKLM' and not is_admin():
                print('  SKIPPED (needs an Administrator terminal):', m['key'], m['name'])
                continue
            with winreg.CreateKeyEx(hive_obj(m['hive']), m['key'], 0, winreg.KEY_SET_VALUE) as k:
                if m['old'] is None:
                    try:
                        winreg.DeleteValue(k, m['name'])
                    except OSError:
                        pass
                    print('  removed value', m['name'])
                else:
                    winreg.SetValueEx(k, m['name'], 0, m['old_type'], m['old'])
                    print('  restored', m['name'])
    print('Done. A reboot is needed for GPU scheduling changes to take effect.')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--undo', nargs='?', const='', default=None, metavar='BACKUP_FOLDER')
    ap.add_argument('--auto-hdr-off', action='store_true')
    ap.add_argument('--steam-dir')
    a = ap.parse_args()
    if os.name != 'nt':
        sys.exit('Windows only.')
    if a.undo is not None:
        return undo(a.undo or None)
    if not a.dry_run and game_running():
        sys.exit('Close Project Zomboid first, then run again.')
    rows, acts, game = build_plan(a)
    print_table(rows)
    nip_here = os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), NIP))
    if any(r[0] == 'NVIDIA profile' and r[3] == 'manual' for r in rows):
        print('\nNVIDIA (manual, optional): download NVIDIA Profile Inspector (github.com/Orbmu2k/nvidiaProfileInspector),\n'
              f'  put {NIP} next to nvidiaProfileInspector.exe and run:  nvidiaProfileInspector.exe -silentImport {NIP}'
              + ('' if nip_here else '\n  (get the .nip from the Oz site tools/ folder)'))
    print('\nNote: Steam "Verify integrity of game files" resets ProjectZomboid64.json; run this again after it.')
    if a.dry_run:
        print('\n--dry-run: nothing was changed.')
        return
    if not acts:
        print('\nNothing to change.')
        return
    if game_running():
        sys.exit('\nClose Project Zomboid first, then run again.')
    print(f'\n{len(acts)} change(s) will be made; everything is backed up first.')
    if input('Type YES to apply: ').strip() != 'YES':
        print('Cancelled, nothing changed.')
        return
    apply(acts)


if __name__ == '__main__':
    main()
