"""Find translations that join observed hCaptcha line pieces end-to-end."""
import itertools
import cv2
import numpy as np
from numpy.typing import NDArray


def thin(image: NDArray[np.bool_]) -> NDArray[np.uint8]:
    a=np.pad(image.astype('uint8'),1)
    while True:
        changed=False
        for step in (0,1):
            p2,p3,p4=a[:-2,1:-1],a[:-2,2:],a[1:-1,2:]
            p5,p6,p7=a[2:,2:],a[2:,1:-1],a[2:,:-2]
            p8,p9=a[1:-1,:-2],a[:-2,:-2]
            neighbors=(p2,p3,p4,p5,p6,p7,p8,p9)
            count=sum(neighbors)
            transitions=sum((neighbors[i]==0)&(neighbors[(i+1)%8]==1) for i in range(8))
            if step==0: allowed=(p2*p4*p6==0)&(p4*p6*p8==0)
            else: allowed=(p2*p4*p8==0)&(p2*p6*p8==0)
            remove=(a[1:-1,1:-1]==1)&(count>=2)&(count<=6)&(transitions==1)&allowed
            if remove.any():
                a[1:-1,1:-1][remove]=0
                changed=True
        if not changed: return a[1:-1,1:-1]


def line_plan(image_bytes: bytes) -> list[dict[str, list[float] | float]]:
    image=cv2.imdecode(np.frombuffer(image_bytes,np.uint8),cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('Unsupported line puzzle dimensions')
    height,width=image.shape[:2]
    if min(height,width)<300 or max(height,width)>2048 or abs(width/height-520/570)>.004:
        raise ValueError('Unsupported line puzzle dimensions')
    # Screenshots use device pixels; all plans remain in the observed frame's
    # 520x570 CSS coordinate system, including at fractional Windows DPI.
    if (height,width)!=(570,520):
        image=cv2.resize(image,(520,570),interpolation=cv2.INTER_AREA)
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    mask=cv2.inRange(hsv,(0,45,0),(179,255,145))
    mask[:163]=0; mask[466:]=0; mask[:,:20]=0; mask[:,495:]=0
    mask[275:300,390:495]=0
    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(13,13)))
    count,labels,stats,centers=cv2.connectedComponentsWithStats(mask)
    objects=[]
    for i in range(1,count):
        if stats[i,4]<=150: continue
        skeleton=thin(labels==i)
        neighbors=cv2.filter2D(skeleton,cv2.CV_16U,np.ones((3,3),np.uint8))
        ys,xs=np.where((skeleton==1)&(neighbors==2))
        if len(xs)!=2: raise ValueError('Ambiguous piece endpoints')
        objects.append({'center':tuple(float(v) for v in centers[i]),
                        'ends':[(float(x),float(y)) for x,y in zip(xs,ys)]})
    movable=[obj for obj in objects if obj['center'][0]>390]
    fixed=[obj for obj in objects if obj['center'][0]<=390]
    if len(movable)!=2 or len(fixed)!=3:
        raise ValueError('Expected two movable and three fixed shapes')
    fixed_ends=[(i,j,np.array(p)) for i,obj in enumerate(fixed) for j,p in enumerate(obj['ends'])]
    options=[]
    for move in movable:
        ends=[np.array(p) for p in move['ends']]
        candidates=[]
        for first,second in itertools.permutations(fixed_ends,2):
            if first[0]==second[0]: continue
            delta_a=first[2]-ends[0]; delta_b=second[2]-ends[1]
            error=float(np.linalg.norm(delta_a-delta_b))
            if error<18:
                candidates.append((error,first[:2],second[:2],(delta_a+delta_b)/2))
        options.append(candidates)
    solutions=[]
    for a,b in itertools.product(*options):
        if len({a[1],a[2],b[1],b[2]})!=4: continue
        # With three fixed pieces and two links, repeating the same pair
        # leaves the third piece disconnected even if all endpoints differ.
        if {a[1][0],a[2][0]}=={b[1][0],b[2][0]}: continue
        solutions.append((a[0]+b[0],a,b))
    solutions.sort(key=lambda x:x[0])
    if not solutions or (len(solutions)>1 and solutions[1][0]-solutions[0][0]<6):
        raise ValueError('Line placement is ambiguous')
    result=[]
    for move,choice in zip(movable,solutions[0][1:]):
        start=np.array(move['center']); end=start+choice[3]
        if not (20<=end[0]<390 and 150<=end[1]<466):
            raise ValueError('Line target is outside the puzzle')
        result.append({'from':start.tolist(),'to':end.tolist(),'error':choice[0]})
    return result

if __name__=='__main__':
    from pathlib import Path
    for f in ('crawlrx-line-visual.jpg','crawlrx-line-visual-2.jpg'):
        print(f,line_plan(Path(f).read_bytes()))
