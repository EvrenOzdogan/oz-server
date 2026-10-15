#!/usr/bin/env python3
"""Oz Server: ZombieBuddy "allow the Oz Server's Java mods" helper (player side).

What it does, and ONLY this:
  1. Reads oz_zb_allow_list.json (next to this script). That file names the Java mod jars the
     Oz Server actually runs (the ZombieBuddy mods enabled in the server's Mods= list), each
     pinned to the exact SHA-256 we reviewed.
  2. Looks in YOUR Steam workshop folder for those Workshop items and prints every jar it found,
     with the Workshop item name, the mod, the jar, its hash, and what it would do:
       allow       - on the Oz list and the hash matches what we reviewed
       ask         - NOT pre-approved (not on the list, or the mod was updated since we
                     reviewed it): ZombieBuddy will show you its own approval window for it
       already ... - you already decided about this exact jar in ZombieBuddy; left as is
  3. Asks you to type YES. Only then it adds an "allow" entry for exactly the "allow" rows to
     ~/.zombie_buddy/mod_approvals.json (a backup of the old file is written first).

There is no flag that skips the YES question. These jars are Java programs that run inside your
game with the same rights as the game. Read the table before you type YES.

Why it is limited: it never touches ZombieBuddy itself, never approves a jar that is not on the
Oz list, and never approves a jar whose hash differs from the one we reviewed. Anything else is
left for ZombieBuddy's own window. ZombieBuddy still does its Steam ban check for every jar.

Close Project Zomboid first (ZombieBuddy rewrites the file while it runs).
Usage:  python oz_zb_allow_pack.py [--dry-run] [--steam-dir DIR] [--list FILE]

Server admins only (rebuild the allow list from this PC's Workshop copies after review):
        python oz_zb_allow_pack.py --build-list --server-mods "ModA;ModB;..." --ids oz_server_mod_ids.json \
            --oz-ids "id1,id2,..." [--pending-out FILE] [--refresh-third-party] [--out FILE]
  Only jars from the --oz-ids items (our own Workshop items) are re-pinned automatically. A changed
  or new THIRD-PARTY jar keeps its old pin (or stays off the list) and is reported as pending for
  manual review; --refresh-third-party applies those changes deliberately.
"""
import argparse, datetime, hashlib, json, os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))


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
    """[(workshop_id, item_dir, jar_path)] for every jar under .../media/java/... in the given items."""
    found, seen = [], set()
    for lib in libs:
        root = os.path.join(lib, 'steamapps', 'workshop', 'content', '108600')
        for wid in sorted(ids):
            base = os.path.join(root, wid)
            if not os.path.isdir(base):
                continue
            for dp, _dn, fn in os.walk(base):
                # Java mods live under .../media/java/...  (same place ZombieBuddy looks)
                if os.sep + 'media' + os.sep + 'java' + os.sep not in dp + os.sep:
                    continue
                for f in fn:
                    if f.lower().endswith('.jar'):
                        rel = os.path.relpath(os.path.join(dp, f), base).replace('\\', '/')
                        if (wid, rel) not in seen:  # an item can sit in several libraries
                            seen.add((wid, rel))
                            found.append((wid, base, os.path.join(dp, f)))
    return found


def mod_folder_info(item_dir, jar_rel):
    """Mod id and name for the mod folder a jar belongs to (mods/<Folder>/...)."""
    parts = jar_rel.split('/')
    if len(parts) < 2 or parts[0] != 'mods':
        return '', ''
    mod_dir = os.path.join(item_dir, 'mods', parts[1])
    for dp, _dn, fn in os.walk(mod_dir):
        if 'mod.info' in fn:
            t = open(os.path.join(dp, 'mod.info'), encoding='utf-8', errors='replace').read()
            i = re.search(r'^\s*id\s*=\s*(.*?)\s*$', t, re.M)
            n = re.search(r'^\s*name\s*=\s*(.*?)\s*$', t, re.M)
            if i:
                return i.group(1), (n.group(1) if n else '')
    return '', ''


def workshop_titles(ids):
    return {k: v['title'] for k, v in workshop_details(ids).items() if v.get('title')}


def _ts(t):
    if not t:
        return '?'
    return datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc).strftime('%Y-%m-%d %H:%MZ')


def workshop_details(ids):
    """Public Steam Web API (no key). Admin build step only; players never call this.
    Returns {id: {'title': str, 'time_updated': int}}."""
    import urllib.parse, urllib.request
    out, ids = {}, list(ids)
    for k in range(0, len(ids), 50):
        chunk = ids[k:k + 50]
        body = {'itemcount': len(chunk)}
        for j, w in enumerate(chunk):
            body[f'publishedfileids[{j}]'] = w
        req = urllib.request.Request(
            'https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/',
            data=urllib.parse.urlencode(body).encode())
        try:
            r = json.load(urllib.request.urlopen(req, timeout=20))
            for d in r['response'].get('publishedfiledetails', []):
                out[d['publishedfileid']] = {'title': d.get('title', ''),
                                             'time_updated': d.get('time_updated', 0)}
        except Exception as e:
            print(f'(could not fetch Workshop details: {e})')
    return out


def build_list(a, libs):
    ids = [str(x) for x in json.load(open(a.ids, encoding='utf-8'))]
    src = a.server_mods
    if os.path.isfile(src):
        src = open(src, encoding='utf-8').read()
    src = re.sub(r'^\s*Mods\s*=', '', src.strip())
    enabled = {m.strip().lstrip('\\') for m in src.split(';') if m.strip()}
    jars = find_jars(libs, set(ids))
    titles = workshop_titles(sorted({w for w, _b, _p in jars}))
    entries, skipped = [], []
    for wid, base, p in jars:
        rel = os.path.relpath(p, base).replace('\\', '/')
        mid, mname = mod_folder_info(base, rel)
        # B42 loads mods/<X>/42/...; a top-level media/java next to a 42 folder is the B41 copy
        parts = rel.split('/')
        legacy = len(parts) > 2 and parts[2] == 'media' and \
            os.path.isdir(os.path.join(base, 'mods', parts[1], '42', 'media', 'java'))
        if mid not in enabled or legacy:
            skipped.append((wid, rel, mid, 'B41 copy' if legacy else 'mod not in Mods='))
            continue
        entries.append({'workshop_id': wid, 'item': titles.get(wid, mname), 'mod_id': mid,
                        'jar': rel, 'sha256': sha256(p), 'signed': os.path.isfile(p + '.zbs')})
    # Security policy: only our own Workshop items (--oz-ids) are re-pinned automatically. A third-party
    # jar whose hash changed keeps its OLD pin, and a new third-party jar stays off the list, until an
    # admin reviews it and reruns with --refresh-third-party.
    prev_doc = json.load(open(a.list, encoding='utf-8')) if os.path.isfile(a.list) else {}
    prev = {(str(e['workshop_id']), e['jar']): e for e in prev_doc.get('jars', [])}
    oz = {x for x in re.split(r'[,;\s]+', a.oz_ids or '') if x}
    final, pending = [], []
    for e in entries:
        o = prev.get((e['workshop_id'], e['jar']))
        if e['workshop_id'] in oz or a.refresh_third_party or (o and o['sha256'] == e['sha256']):
            final.append(e)
        elif o:
            final.append(o)
            pending.append(('changed', e, o))
        else:
            pending.append(('new', e, None))
    newkeys = {(e['workshop_id'], e['jar']) for e in entries}
    dropped = [o for k, o in prev.items() if k not in newkeys]
    final.sort(key=lambda e: (e['item'].lower(), e['jar'].lower()))
    gen = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if prev_doc.get('jars') == final and prev_doc.get('generated'):
        gen = prev_doc['generated']  # nothing changed: keep the file byte-identical
    out = {'note': 'Java jars the Oz Server runs (ZombieBuddy mods enabled in Mods=), pinned to the '
                   'reviewed SHA-256. Rebuild with --build-list after mod updates.',
           'generated': gen,
           'jars': final}
    dest = a.out or a.list
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write('\n')
    print(f'Wrote {len(final)} jars to {dest}. Left out ({len(skipped)}):')
    for s in skipped:
        print('   ', *s)
    for o in dropped:
        print(f'Removed from list (no longer found/enabled): {o["workshop_id"]} {o["item"]} {o["jar"]}')
    det = workshop_details(sorted({e['workshop_id'] for _k, e, _o in pending})) if pending else {}
    rows = []
    for kind, e, o in pending:
        rows.append((e['workshop_id'], e['item'], e['jar'], o['sha256'][:12] if o else '(not on list)',
                     e['sha256'][:12], _ts(det.get(e['workshop_id'], {}).get('time_updated')), kind))
    if rows:
        print(f'PENDING third-party jar changes NOT applied ({len(rows)}); review, then rerun with '
              f'--refresh-third-party:')
        for r in rows:
            print(f'  PENDING {r[0]} {r[1][:50]} | {r[2].rsplit("/", 1)[-1]} | {r[3]} -> {r[4]} '
                  f'| Workshop updated {r[5]} | {r[6]}')
    elif a.refresh_third_party:
        print('PENDING: none (third-party jars refreshed with --refresh-third-party).')
    else:
        print('PENDING: none (no third-party jar changes).')
    if a.pending_out:
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
        lines = ['# ZombieBuddy allow list: pending third-party jar changes', '',
                 f'Last build: {now}' + (' (--refresh-third-party: all applied)' if a.refresh_third_party else ''),
                 '',
                 'Third-party jars whose hash changed (or that are new) are NOT re-pinned automatically. The list '
                 "keeps the old hash, so players get ZombieBuddy's own approval window for the updated jar. "
                 'Review each one, then rebuild with `--refresh-third-party` '
                 '(ship_mods.sh: `REFRESH_THIRD_PARTY=1`).', '']
        if rows:
            lines += ['| Workshop id | Item | Jar | Old hash | New hash | Workshop updated (UTC) | Kind |',
                      '|---|---|---|---|---|---|---|']
            lines += ['| ' + ' | '.join(str(c).replace('|', '/') for c in r) + ' |' for r in rows]
        else:
            lines.append('None pending.')
        with open(a.pending_out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'Pending report: {a.pending_out}')


def main():
    for _s in (sys.stdout, sys.stderr):
        try: _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='show the table, change nothing')
    ap.add_argument('--steam-dir')
    ap.add_argument('--list', default=os.path.join(HERE, 'oz_zb_allow_list.json'))
    ap.add_argument('--config-dir', default=os.path.join(os.path.expanduser('~'), '.zombie_buddy'))
    ap.add_argument('--build-list', action='store_true', help='server admins: rebuild the allow list')
    ap.add_argument('--server-mods', help='with --build-list: the Mods= value (or a file holding it)')
    ap.add_argument('--ids', default=os.path.join(HERE, 'oz_server_mod_ids.json'),
                    help='with --build-list: Workshop ids of the server mod list')
    ap.add_argument('--oz-ids', help='with --build-list: our own Workshop ids (comma list); only these '
                                     're-pin automatically')
    ap.add_argument('--refresh-third-party', action='store_true',
                    help='with --build-list: also apply third-party jar hash changes (after review)')
    ap.add_argument('--pending-out', help='with --build-list: write the pending third-party report (Markdown)')
    ap.add_argument('--out', help='with --build-list: write the list here instead of over --list')
    a = ap.parse_args()

    libs = steam_libraries(a.steam_dir)
    if not libs:
        sys.exit('Could not find your Steam folder. Pass --steam-dir "C:\\path\\to\\Steam".')
    if a.build_list:
        if not a.server_mods:
            sys.exit('--build-list needs --server-mods')
        return build_list(a, libs)

    if not os.path.isfile(a.list):
        sys.exit(f'Missing {a.list}. Download oz_zb_allow_list.json next to this script.')
    allow = json.load(open(a.list, encoding='utf-8'))['jars']
    pinned = {(str(e['workshop_id']), e['jar']): e for e in allow}
    jars = find_jars(libs, {str(e['workshop_id']) for e in allow})

    appr_path = os.path.join(a.config_dir, 'mod_approvals.json')
    data = {'formatVersion': 2, 'mods': []}
    if os.path.isfile(appr_path):
        data = json.load(open(appr_path, encoding='utf-8'))
        data.setdefault('mods', [])
    have = {m.get('jar_hash'): m for m in data['mods']}

    rows, todo = [], []
    for wid, base, p in jars:
        rel = os.path.relpath(p, base).replace('\\', '/')
        e = pinned.get((wid, rel))
        h = sha256(p)
        cur = have.get(h)
        if cur and cur.get('decision') is True:
            state = 'already allowed'
        elif cur and cur.get('decision') is False:
            state = 'already DENIED (kept)'
        elif e is None:
            state = 'ask (not on Oz list)'
        elif e['sha256'] != h:
            state = 'ask (updated since review)'
        else:
            state = 'allow'
            todo.append((wid, p, h))
        item = e['item'] if e else mod_folder_info(base, rel)[1]
        rows.append((wid, item, os.path.basename(p), h[:12], state))
    missing = [e for e in allow if (str(e['workshop_id']), e['jar']) not in {(w, os.path.relpath(p, b).replace('\\', '/')) for w, b, p in jars}]

    print(f'Steam libraries searched: {libs}')
    print(f'Oz allow list: {len(allow)} jars; found on this PC: {len(jars)}\n')
    print(f'{"workshop":<12}{"item":<40}{"jar":<36}{"sha256":<14}action')
    for r in rows:
        print(f'{r[0]:<12}{r[1][:38]:<40}{r[2][:34]:<36}{r[3]:<14}{r[4]}')
    if missing:
        print(f'\nNot downloaded yet ({len(missing)}): ' + ', '.join(e['item'] for e in missing))
        print('Subscribe/join once so Steam downloads them, then run this again.')
    print(f'\n{len(todo)} jar(s) would be newly allowed ("allow" rows). Everything else is untouched;')
    print('ZombieBuddy will show its own window for "ask" rows.')
    if a.dry_run or not todo:
        print('Nothing written.')
        return
    try:
        ans = input('These are Java programs that run inside your game. Allow exactly the "allow" rows? Type YES: ')
    except EOFError:
        ans = ''
    if ans.strip() != 'YES':
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
