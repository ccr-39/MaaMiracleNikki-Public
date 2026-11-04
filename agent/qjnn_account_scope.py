"""Scope unresolved-input journals to a twice-observed game nameplate.

This is not daily progress: completed business state always comes from game UI.
No names are logged or stored; ambiguous/absent nameplates use the conservative
legacy fence. A legacy unresolved operation is never silently reassigned.
"""
import hashlib
import json
from pathlib import Path


def nameplate(items,frame):
    h,w=frame.shape[:2]
    values=[]
    for item in items:
        box=item.box;text=''.join(item.text.split())
        if (item.score>=.98 and 1<=len(text)<=24 and not text.isdigit()
                and .02*w<=box.x<box.right<=.25*w
                and .08*h<=box.y<box.bottom<=.135*h):
            values.append(text)
    return values[0] if len(values)==1 else None


def account_record_path(session,legacy):
    legacy=Path(legacy)
    # Old unscoped uncertainty cannot be attributed to the visible account.
    # Keep the old gate rather than silently bypassing it with a new namespace.
    if legacy.with_suffix(legacy.suffix+'.lock').exists() or legacy.with_suffix('.lock').exists():
        return legacy
    if legacy.exists():
        value=json.loads(legacy.read_text(encoding='utf-8'))
        if not isinstance(value,dict):return legacy
        if (value.get('state')=='pending' or value.get('status')=='pending'
                or value.get('pending') is not None):return legacy
    names=[]
    for index in range(2):
        frame=session.capture_frame()
        names.append(nameplate(session.recognize_text(frame),frame))
        if index==0:session.sleep(.4)
    if names[0] is None or names[0]!=names[1]:
        # A restart inside a subpage can hide the nameplate. Do not bypass an
        # account-specific uncertain input merely by falling back to a new file.
        accounts=legacy.parent/'accounts'
        for folder in accounts.iterdir() if accounts.exists() else []:
            candidate=folder/legacy.name
            if candidate.with_suffix(candidate.suffix+'.lock').exists() or candidate.with_suffix('.lock').exists():
                raise ValueError('账号无法确认且存在未决操作锁，禁止重试')
            if candidate.exists():
                value=json.loads(candidate.read_text(encoding='utf-8'))
                if not isinstance(value,dict) or value.get('state')=='pending' or value.get('status')=='pending' or value.get('pending') is not None:
                    raise ValueError('账号无法确认且存在未决操作，禁止重试')
        return legacy
    key=hashlib.sha256(names[0].encode('utf-8')).hexdigest()
    return legacy.parent/'accounts'/key/legacy.name
