"""Exchange-anchored monotonic timing. No networking or implicit clock repair."""
import time


class Clock:
    def __init__(self, server_ms, request_start_ns, *, mono=time.monotonic_ns, wall=time.time_ns):
        self.mono,self.wall=mono,wall
        end=mono()
        if end<request_start_ns or end-request_start_ns>10_000_000_000:
            raise ValueError('Public time request duration invalid')
        self.origin=end
        self.wall_origin=wall()//1_000_000
        if abs(self.wall_origin-server_ms)>30000: raise ValueError('Exchange/local clock disagreement')
        # Conservative upper bound includes full request RTT, rather than making
        # a slow response appear to have been durably processed earlier.
        self.anchor=server_ms+(end-request_start_ns+999999)//1_000_000
        self.last=end

    def now(self):
        n=self.mono(); w=self.wall()//1_000_000
        if n<self.last or abs((w-self.wall_origin)-(n-self.origin)//1_000_000)>30000:
            raise ValueError('Wall/monotonic clock disagreement')
        self.last=n
        return self.anchor+(n-self.origin+999999)//1_000_000

    def reconcile(self, server_ms):
        if abs(self.now()-server_ms)>30000: raise ValueError('Exchange/monotonic clock disagreement')


def classify(boundary, persisted_ms, tolerance=300000):
    if persisted_ms<boundary: raise ValueError('Observation before boundary')
    return 'ONLINE' if persisted_ms-boundary<=tolerance else 'DELAYED_RECONSTRUCTED'
