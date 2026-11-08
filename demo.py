"""Run one incident end to end against the fakes and print what FURY did.  python demo.py [disk_full|service_down|cpu_runaway|memory_pressure]"""
import sys
from fury import system
from fury.adapters import FakeTarget, FakeSnow, FakeChat
fault = sys.argv[1] if len(sys.argv) > 1 else "disk_full"
t = FakeTarget(); t.inject(fault); s = FakeSnow(); c = FakeChat([("U0CAZH32V2T", "approve")])
bus, _, _ = system.build(target=t, snow=s, chat=c)
out = system.run_alert(bus, system.ALERTS[fault]).output
print("OUTCOME:", out)
for n, i in s.incidents.items():
    print(f"\n=== {n} | state={i['state']} | group={i['assignment_group'] or '-'} | {i['short_description']}")
    for w in i["work_notes"]: print("  NOTE:", w.replace("\n", "\n        "))
    if i.get("close_notes"): print("  CLOSE:", i["close_notes"].replace("\n", "\n        "))
print("\nCHAT:"); [print(" ", ch, "|", m.replace("\n", " / ")) for ch, m in c.messages]
print("\nA2A TRACE:"); [print(f"  {x['from']:>11} -> {x['to']:<11} {x['skill']:<18} {x['state']}") for x in bus.trace]
