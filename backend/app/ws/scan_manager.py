

@dataclass
class ScanConnection:
    websocket: WebSocket
    user_id: str
    role: str
    device_id: Optional[str]
    device: Optional[Device] = None
    tracker: "IoUTracker" = field(default_factory=lambda: IoUTracker())
    last_frame_time: float = 0.0
    frame_processing: bool = False
    ocr_tasks: Set[asyncio.Task] = field(default_factory=set)
    recent_plates: deque = field(default_factory=lambda: deque(maxlen=20))
    track_seen: Dict[int, int] = field(default_factory=dict)


class IoUTracker:
    def __init__(self, max_age: int = 10, iou_thresh: float = 0.3):
        self.next_id = 0
        self.tracks: Dict[int, Dict[str, Any]] = {}
        self.max_age = max_age
        self.iou_thresh = iou_thresh

    def update(self, boxes_cls: List[Tuple[Tuple[float, float, float, float], int, float]]) -> List[Dict[str, Any]]:
        if not boxes_cls:
            for tid in list(self.tracks.keys()):
                self.tracks[tid]["age"] += 1
                if self.tracks[tid]["age"] > self.max_age:
                    del self.tracks[tid]
            return []
        unused = set(self.tracks.keys())
        matches: List[Tuple[int, int, float]] = []
        dets = boxes_cls
        for i, (dbox, dcls, dconf) in enumerate(dets):
            best_tid = None
            best_iou = 0.0
            for tid, tr in self.tracks.items():
                iou = compute_iou(tr["box"], dbox)
                if iou > best_iou:
                    best_iou = iou
                    best_tid = tid
            if best_tid is not None and best_iou >= self.iou_thresh:
                matches.append((best_tid, i, best_iou))
                if best_tid in unused:
                    unused.discard(best_tid)
        matches.sort(key=lambda x: x[2], reverse=True)
        used_tids = set()
        used_dets = set()
        for tid, di, _ in matches:
            if tid in used_tids or di in used_dets:
                continue
            used_tids.add(tid)
            used_dets.add(di)
            tr = self.tracks[tid]
            tr["box"] = dets[di][0]
            tr["conf"] = dets[di][2]
            tr["age"] = 0
            tr["hits"] += 1
        for di in range(len(dets)):
            if di in used_dets:
                continue
            tid = self.next_id
            self.next_id += 1
            dbox, dcls, dconf = dets[di]
            self.tracks[tid] = {"tid": tid, "box": dbox, "cls": dcls, "conf": dconf, "age": 0, "hits": 0}
            used_tids.add(tid)
        for tid in list(unused):
            self.tracks[tid]["age"] += 1
            if self.tracks[tid]["age"] > self.max_age:
                del self.tracks[tid]
        out = []
        for tid, tr in list(self.tracks.items()):
            out.append({"track": tid, "box": tr["box"], "cls": tr["cls"], "conf": tr["conf"], "hits": tr["hits"], "age": tr["age"]})
        out.sort(key=lambda x: x["track"])
        return out
