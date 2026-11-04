"""Optional signup, selected from OCR and list geometry, with a durable submit fence."""
from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import cv2
from app_paths import DATA_ROOT
from qjnn_alliance_commission import Rect
from qjnn_alliance_click import alliance_business_click
from qjnn_common_popup import detect_common_safe_popup, CommonPopupKind
from qjnn_competition import find_competition_home
from qjnn_competition_schedule import (competition_time_restricted, COMPETITION_TIME_NOTICE,
                                       CompetitionTimeRestricted)

RECORD = DATA_ROOT / 'state' / 'competition_registration.json'


class SubmitFence:
    def __init__(self, path, today):
        self.path=Path(path);self.today=today;self.value=None

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        lock=self.path.with_suffix('.lock')
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        try:
            if self.path.exists():
                self.value=json.loads(self.path.read_text(encoding='utf-8'))
                v=self.value
                if (not isinstance(v,dict) or v.get('status') not in {'pending','done'}
                        or type(v.get('version')) is not int or v['version']!=1
                        or not isinstance(v.get('date'),str)
                        or date.fromisoformat(v['date'])>self.today):
                    raise ValueError('invalid signup fence')
            yield self
        finally:
            lock.unlink()

    def save(self,status):
        tmp=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.path.parent,delete=False,suffix='.tmp') as stream:
                tmp=Path(stream.name)
                json.dump(dict(version=1,date=self.today.isoformat(),status=status),stream)
                stream.flush();os.fsync(stream.fileno())
            os.replace(tmp,self.path)
            self.value=dict(version=1,date=self.today.isoformat(),status=status)
        finally:
            if tmp is not None:tmp.unlink(missing_ok=True)


def label(items,frame,text,roi=(0,0,1,1)):
    found=[i for i in items if ''.join(i.text.split())==text]
    if len(found)!=1 or found[0].score<.9:return None
    b=found[0].box;h,w=frame.shape[:2];l,t,r,d=roi
    if not (w*l<=b.x<b.right<=w*r and h*t<=b.y<b.bottom<=h*d):return None
    inset=max(1,min(b.width,b.height)//6)
    if min(b.width,b.height)<=2*inset:return None
    return Rect(b.x+inset,b.y+inset,b.width-2*inset,b.height-2*inset)


def clear(items,frame):
    return (detect_common_safe_popup(frame,items).kind is CommonPopupKind.NONE
            and not any(''.join(i.text.split()) in {'确定','确认','取消','恭喜获得','获得奖励'} for i in items))


def dressing(items,frame):
    return (clear(items,frame) and label(items,frame,'换好了',(.05,.65,.95,1)) is not None
            and label(items,frame,'套装',(.55,0,1,1)) is not None)


def first_suit(items,frame):
    """Locate the first suit by row markers or borders, never by its name."""
    if not dressing(items,frame):return None
    heading=(label(items,frame,'我的套装',(.6,0,1,.5))
             or label(items,frame,'套装列表',(.6,0,1,.5))
             or label(items,frame,'套装',(.6,0,1,.5)))
    h,w=frame.shape[:2]
    # Current game uses an unframed vertical list headed 默认分类. Locate its
    # first slot from the column and row markers, independent of suit names/art.
    # My Suits can retain 标签分类 as its header, even though the category
    # menu has already closed. The first-row ordinal distinguishes the list.
    default=(label(items,frame,'默认分类',(.7,0,1,.15))
             or label(items,frame,'标签分类',(.7,0,1,.15)))
    if default is not None:
        ranks={}
        for i in items:
            if i.score>=.9 and ''.join(i.text.split()) in {'1','2'} and .7*w<i.box.x<.77*w:
                ranks.setdefault(''.join(i.text.split()),[]).append(i.box)
        if len(ranks.get('1',[]))==1 and len(ranks.get('2',[]))<=1:
            one=ranks['1'][0]
            two=ranks.get('2',[None])[0]
            if default.bottom<one.y<h*.3 and (two is None or h*.1<two.y-one.y<h*.3):
                top=default.bottom+max(4,round(.01*h))
                bottom=one.y+round(.12*h)
                if two is not None:bottom=min(bottom,two.y-round(.04*h))
                if top<one.y<bottom:
                    return Rect(round(.76*w),top,round(.21*w),bottom-top)
        return None
    if heading is None:return None
    edges=cv2.Canny(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),60,150)
    contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    rows=[]
    for contour in contours:
        x,y,bw,bh=cv2.boundingRect(contour)
        if (x>=w*.65 and x+bw<=w*.99 and w*.18<=bw<=w*.34
                and heading.bottom<y<h*.5 and h*.07<=bh<=h*.23
                and y+bh<h*.78):
            hull=cv2.approxPolyDP(contour,.02*cv2.arcLength(contour,True),True)
            if len(hull)==4:rows.append(Rect(x,y,bw,bh))
    if not rows:return None
    rows.sort(key=lambda b:(b.y,b.x))
    first=rows[0]
    # Ignore nested borders of the same first cell, reject two columns at its top.
    if any(abs(b.y-first.y)<h*.025 and abs(b.x-first.x)>w*.04 for b in rows[1:]):return None
    margin=max(3,min(first.width,first.height)//5)
    return Rect(first.x+margin,first.y+margin,first.width-2*margin,first.height-2*margin)


def snapshot(session):
    frame=session.capture_frame();return frame,session.recognize_text(frame)


def stable_click(session,selector,before=None):
    def target(items,frame):
        box=selector(items,frame)
        if box is None:return None
        x,y=box.center
        if any('分享' in i.text and i.box.x-5<=x<=i.box.right+5 and i.box.y-5<=y<=i.box.bottom+5 for i in items):return None
        return box
    def confirm():
        if competition_time_restricted():
            raise CompetitionTimeRestricted
        frame,items=snapshot(session);old=target(items,frame)
        if old is None:return None
        session.sleep(.5)
        frame,items=snapshot(session);new=target(items,frame)
        if new is None or any(abs(a-b)>3 for a,b in zip((old.x,old.y,old.width,old.height),(new.x,new.y,new.width,new.height))):return None
        if competition_time_restricted():
            raise CompetitionTimeRestricted
        if before is not None:before()
        return new
    return alliance_business_click(session,0,0,confirm=confirm)


def wait(session,predicate):
    previous=None
    for _ in range(30):
        frame,items=snapshot(session);value=predicate(items,frame)
        if value is not None and value==previous:return frame,items,value
        previous=value;session.sleep(.4)
    return None


def run_registration(session,*,record_path=RECORD,today=None,capture=None,log=print):
    if competition_time_restricted():
        log(f'[CompetitionEnter] {COMPETITION_TIME_NOTICE} 本次跳过参赛。')
        return True
    local_day=today or date.today()
    def fail(reason):
        log(f'[CompetitionEnter] SafeStop: {reason}; no submit retry')
        if capture is not None:
            try:
                frame,_=snapshot(session);capture('QJNN_CompetitionUnknownStart',frame)
            except Exception:pass
        return False
    def home(items,frame):
        return find_competition_home(items,frame) is not None and clear(items,frame)
    try:
        def entry(items,frame):
            if not home(items,frame):return None
            start=label(items,frame,'我要参赛',(.05,.4,.95,1))
            changed=label(items,frame,'更换搭配',(.05,.4,.95,1))
            if start is not None and changed is not None:return None
            return ('joined',changed) if changed is not None else ('start',start) if start is not None else None
        ready=wait(session,entry)
        if ready is None:return fail('entry unknown')
        # Joined is authoritative and read-only; another account's unresolved
        # signup must not prevent judging this already-registered account.
        if ready[2][0]=='joined':return True
        if getattr(session,'account_scoped_records',False) and Path(record_path)==RECORD:
            from qjnn_account_scope import account_record_path
            record_path=account_record_path(session,RECORD)
        with SubmitFence(record_path,local_day).transaction() as fence:
            if fence.value is not None and fence.value['status']=='pending':return fail('unresolved submit')
            if not stable_click(session,lambda its,img: entry(its,img)[1] if entry(its,img) is not None and entry(its,img)[0]=='start' else None):return fail('entry changed')
            if wait(session,lambda its,img: True if dressing(its,img) else None) is None:return fail('dressing unknown')
            if not stable_click(session,lambda its,img: label(its,img,'套装',(.55,0,1,1)) if dressing(its,img) else None):return fail('suit tab unknown')
            layout=wait(session,lambda its,img: ('tags',label(its,img,'我的套装',(.6,0,1,.6))) if dressing(its,img) and label(its,img,'标签分类') is not None and label(its,img,'我的套装',(.6,0,1,.6)) is not None else ('list',first_suit(its,img)) if first_suit(its,img) is not None else None)
            if layout is None:return fail('list unknown')
            if layout[2][0]=='tags':
                if not stable_click(session,lambda its,img: label(its,img,'我的套装',(.6,0,1,.6)) if dressing(its,img) and label(its,img,'标签分类') is not None else None):return fail('my suits changed')
            selected=wait(session,first_suit)
            if selected is None:return fail('first row unknown')
            first=selected[2]
            selected_once=False
            def worn(items,frame):
                if not dressing(items,frame):return None
                h,w=frame.shape[:2]
                roi=(first.x/w,first.y/h,first.right/w,first.bottom/h)
                # Selecting a suit opens its equipped-parts list. It no longer
                # has 默认分类 or the original first-suit layout; do not require
                # that pre-click layout again to recognize the enabled Done.
                explicit=label(items,frame,'已穿戴',roi) if first_suit(items,frame)==first else None
                if explicit is not None:return explicit
                done=label(items,frame,'换好了',(.05,.65,.95,1))
                if not selected_once or done is None:return None
                # Acknowledged first-slot click plus the now-enabled pink Done
                # button is the game's wear result; names are never inspected.
                crop=frame[max(0,done.y-10):min(h,done.bottom+10),max(0,done.x-12):min(w,done.right+12)]
                if not crop.size:return None
                b,g,r=cv2.split(crop.astype('int16'))
                return first if ((r>180)&(r-g>35)&(r-b>20)).mean()>.18 else None
            if worn(selected[1],selected[0]) is None:
                if not stable_click(session,lambda its,img: first_suit(its,img) if first_suit(its,img)==first else None):return fail('first row changed')
                selected_once=True
            if wait(session,worn) is None:return fail('wear unconfirmed')
            def submit(items,frame):
                return label(items,frame,'换好了',(.05,.65,.95,1)) if dressing(items,frame) and worn(items,frame) is not None else None
            def arm():
                if today is None and date.today()!=local_day:raise ValueError('calendar changed')
                fence.save('pending')
                if today is None and date.today()!=local_day:raise ValueError('calendar changed before submit')
            if not stable_click(session,submit,arm):return fail('submit uncertain')
            # A transient toast is observed, never clicked. The changed button is authoritative.
            def joined(items,frame):
                if any('报名成功' in i.text for i in items):return None
                return label(items,frame,'更换搭配',(.05,.4,.95,1)) if home(items,frame) and label(items,frame,'我要参赛') is None else None
            if wait(session,joined) is None:return fail('submit result unconfirmed')
            fence.save('done');log('[CompetitionEnter] joined; continue judging')
            return True
    except CompetitionTimeRestricted:
        log(f'[CompetitionEnter] {COMPETITION_TIME_NOTICE} 已停止参赛操作。')
        return True
    except Exception as exc:return fail(str(exc))
