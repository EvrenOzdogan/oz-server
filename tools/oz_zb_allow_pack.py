#!/usr/bin/env python3
"""Oz Server: one-step ZombieBuddy "allow the Oz modpack's Java mods" helper (player side).

What it does, and ONLY this:
  1. Looks in YOUR Steam workshop folder for the Java mod jars that belong to the Oz Server mod list
     (workshop ids from oz_server_mod_ids.json next to this script, or --ids FILE).
  2. Prints every jar it found with its SHA-256 and whether it is already approved.
  3. Asks you to type YES. Only then it adds an "allow" entry for exactly those jar hashes to
     ~/.zombie_buddy/mod_approvals.json (a backup of the old file is written first).

Why it is safe to read: it never touches ZombieBuddy itself, never approves a jar that is not in the
server's mod list, and the approval is tied to the jar's hash, so an UPDATED jar (new hash) will
still make ZombieBuddy ask you again. ZombieBuddy still does its Steam ban check for every jar.

Close Project Zomboid first (ZombieBuddy rewrites the file while it runs).
Usage:  python oz_zb_allow_pack.py [--dry-run] [--steam-dir DIR] [--ids FILE] [--yes]
"""
import argparse, datetime, hashlib, json, os, re, shutil, sys


def steam_libraries(steam_dir=None):
    cands = []
    if steam_dir:
        cands.append(steam_dir)
    if os.name == 'nt':
        for k in (r'C:\Program Files (x86)\Steam', r'C:\Program Files\Steam'):
            cands.append(k)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                cands.append(winreg.QueryValueEx(key, 'SteamPath')[0].replace('/', '\\'))
        except Exception:
            pass
    else:
        home = os.path.expanduser('~')
        cands += [home + '/.steam/steam', home + '/.local/share/Steam',
                  home + '/Library/Application Support/Steam']
    libs = []
    for c in cands:
        if not os.path.isdir(c):
            continue
        libs.append(c)
        vdf = os.path.join(c, 'steamapps', 'libraryfolders.vdf')
        if os.path.isfile(vdf):
            for m in re.finditer(r'"path"\s+"([^"]+)"', open(vdf, encoding='utf-8', errors='replace').read()):
                libs.append(m.group(1).replace('\\\\', '\\'))
    seen, out = set(), []
    for l in libs:
        k = os.path.normcase(os.path.abspath(l))
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def find_jars(libs, ids):
    found = []
    for lib in libs:
        root = os.path.join(lib, 'steamapps', 'workshop', 'content', '108600')
        for wid in ids:
            base = os.path.join(root, wid)
            if not os.path.isdir(base):
                continue
            for dp, _dn, fn in os.walk(base):
                # Java mods live under .../media/java/...  (same place ZombieBuddy looks)
                if os.sep + 'media' + os.sep + 'java' not in dp + os.sep:
                    continue
                for f in fn:
                    if f.lower().endswith('.jar'):
                        found.append((wid, os.path.join(dp, f)))
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='show what would be approved, change nothing')
    ap.add_argument('--steam-dir')
    ap.add_argument('--ids', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oz_server_mod_ids.json'))
    ap.add_argument('--config-dir', default=os.path.join(os.path.expanduser('~'), '.zombie_buddy'))
    ap.add_argument('--yes', action='store_true', help='skip the YES prompt (for scripted tests only)')
    a = ap.parse_args()

    ids = [str(x) for x in json.load(open(a.ids, encoding='utf-8'))]
    libs = steam_libraries(a.steam_dir)
    if not libs:
        sys.exit('Could not find your Steam folder. Pass --steam-dir "C:\\path\\to\\Steam".')
    jars = find_jars(libs, set(ids))
    # a mod id can sit in several libraries; keep the first of each path
    jars = sorted({p: (w, p) for w, p in jars}.values(), key=lambda t: t[1].lower())

    appr_path = os.path.join(a.config_dir, 'mod_approvals.json')
    data = {'formatVersion': 2, 'mods': []}
    if os.path.isfile(appr_path):
        data = json.load(open(appr_path, encoding='utf-8'))
        data.setdefault('mods', [])
    have = {m.get('jar_hash'): m for m in data['mods']}

    rows, todo = [], []
    for wid, p in jars:
        h = sha256(p)
        signed = os.path.isfile(p + '.zbs')
        cur = have.get(h)
        state = 'already allowed' if cur and cur.get('decision') is True else \
                'DENIED earlier (kept as is)' if cur and cur.get('decision') is False else 'NEW -> allow'
        rows.append((wid, os.path.basename(p), h[:12], 'signed' if signed else 'unsigned', state))
        if state.startswith('NEW'):
            todo.append((wid, p, h))
    print(f'Steam libraries searched: {libs}')
    print(f'Oz mod list: {len(ids)} workshop items, {len(jars)} Java mod jars installed on this PC\n')
    print(f'{"workshop":<12}{"jar":<44}{"sha256":<14}{"zbs":<9}state')
    for r in rows:
        print(f'{r[0]:<12}{r[1][:42]:<44}{r[2]:<14}{r[3]:<9}{r[4]}')
    print(f'\n{len(todo)} jar(s) would be newly allowed; the rest are untouched.')
    if not todo or a.dry_run:
        print('Nothing written.' if a.dry_run or not todo else '')
        return
    if not a.yes:
        if input('These are Java programs that run inside your game. Allow exactly the NEW ones above? Type YES: ').strip() != 'YES':
            sys.exit('Cancelled. Nothing written.')
    os.makedirs(a.config_dir, exist_ok=True)
    if os.path.isfile(appr_path):
        shutil.copy2(appr_path, appr_path + '.bak_ozallow_' + datetime.datetime.now().strftime('%Y%m%d%H%M%S'))
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for wid, p, h in todo:
        data['mods'].append({'id': os.path.splitext(os.path.basename(p))[0], 'workshop_id': int(wid),
                             'jar_hash': h, 'decision': True, 'time': now})
    tmp = appr_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, appr_path)
    print(f'Done: {len(todo)} entries added to {appr_path}. Start the game; ZombieBuddy will not ask about them again.')


if __name__ == '__main__':
    main()
