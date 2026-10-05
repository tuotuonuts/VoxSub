import { execFile } from "node:child_process";
import type { ShapeRect } from "../shared/overlay-shape";
export interface NativeOverlayCheck { regionVerified: boolean; materialVerified: boolean; desktop: "not_run"; dpi?: number; backdrop?: number; error?: string }
export function probeNativeOverlay(launch: { command: string; args: string[] }, hwnd: Buffer, shape: ShapeRect[], material: boolean): Promise<NativeOverlayCheck> {
  const request=JSON.stringify({hwnd:hwnd.length===8?hwnd.readBigUInt64LE().toString():String(hwnd.readUInt32LE()),pid:process.pid,shape,material});
  return new Promise((resolve,reject)=>{
    execFile(launch.command,[...launch.args,"--overlay-native",request],{
      windowsHide:true,timeout:4000,maxBuffer:65536,encoding:"utf8",
      env:{...process.env,PYTHONHOME:"",PYTHONPATH:"",PYTHONIOENCODING:"utf-8"},
    },(error,stdout)=>{
      if(error){reject(new Error("native_probe_unavailable"));return;}
      try {const result=JSON.parse(stdout) as NativeOverlayCheck;
        if(typeof result.regionVerified!=="boolean"||typeof result.materialVerified!=="boolean")throw Error("invalid_native_reply");
        resolve({...result,desktop:"not_run"});
      }catch {reject(new Error("invalid_native_reply"));}
    });
  });
}
