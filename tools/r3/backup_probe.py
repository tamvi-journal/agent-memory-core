"""Synthetic-only cross-platform timestamp diagnostic; never an acceptance gate."""
from pathlib import Path
import json, os, shutil, subprocess, sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.r3.common import isolated_env

root = ROOT / ".isolation" / "backup-probe"
env = isolated_env(root)
program = r"""
const {statSync,copyFileSync,utimesSync} = await import("node:fs");
const source=process.argv[1],target=process.argv[2];
const ns=statSync(source,{bigint:true}).mtimeNs;
const before={ns:String(ns),ms:String(statSync(source,{bigint:true}).mtimeMs),numberMs:statSync(source).mtimeMs};
copyFileSync(source,target);
const copied=String(statSync(target,{bigint:true}).mtimeNs);
const sec=ns/1000000000n,micros=(ns%1000000000n)/1000n;
const midpoint=Number(sec)+Number(micros*1000n+500n)/1e9;
utimesSync(target,statSync(source).atime,midpoint);
console.log(JSON.stringify({before,copied,midpoint,set:String(statSync(target,{bigint:true}).mtimeNs)}));
"""
for index, requested in enumerate([0,-1,1700000000123456789,(1<<32)*10**9-1,(1<<32)*10**9]):
    source, target = root / f"source-{index}", root / f"backup-{index}"
    source.write_bytes(b"synthetic timestamp only")
    os.utime(source,ns=(0,requested))
    result = subprocess.run([shutil.which("node"),"--input-type=module","-e",program,str(source),str(target)],cwd=root,env=env,capture_output=True,check=True)
    print(json.dumps({"requested_ns":requested,"python_ns":source.stat().st_mtime_ns,"node":json.loads(result.stdout)}),flush=True)

from tools.r3.run import run_case
for runtime in ("py","ts"):
    case_root=root/("profile-"+runtime)
    isolated_env(case_root)
    result=run_case(runtime,case_root,{"surface":"cli","argv":["--profile","example","--db",str(case_root/"store.sqlite3"),"status"]})
    print(json.dumps({"runtime":runtime,"exit":result["exit"],"last_profile_hex":(case_root/"data/.last-profile").read_bytes().hex()}),flush=True)
