/** One window owns geometry, verification and fallback; late helpers cannot resurrect it. */
import { OverlaySurface, type SurfaceWindow } from "./overlay-surface";
import { frameShape, type ShapeRect } from "../shared/overlay-shape";
import type { OverlayFrame } from "../shared/overlay-frame";
import type { OverlayGlassState } from "../shared/overlay-glass";
import type { NativeOverlayCheck } from "./overlay-native";
export type SurfaceEvidence = Pick<OverlayGlassState,"active"|"clippingCheck"|"materialCheck"|"desktopCheck"|"fallbackReason">;
export class VerifiedOverlaySurface {
  private readonly surface=new OverlaySurface();
  private timer: ReturnType<typeof setTimeout> | null=null;
  private generation=0;
  private disposed=false;
  private blocked=false;
  private nativeApplied=false;
  private last="";
  private shape: ShapeRect[] | null=null;
  evidence: SurfaceEvidence={active:false,clippingCheck:"not_run",materialCheck:"not_run",desktopCheck:"not_run",fallbackReason:null};
  constructor(private readonly win:SurfaceWindow,
    private readonly probe:(shape:ShapeRect[],material:boolean)=>Promise<NativeOverlayCheck>,
    private readonly publish:(evidence:SurfaceEvidence)=>void,
    private readonly unsafe:()=>void) {}
  private report(patch:Partial<SurfaceEvidence>) { this.evidence={...this.evidence,...patch,desktopCheck:"not_run"};this.publish(this.evidence); }
  reset() {this.blocked=false;this.last="";}
  update(enabled:boolean, frame:OverlayFrame|null, scale=1, force=false) {
    if(this.disposed)return;
    const [width,height]=this.win.getContentSize(),zoom=this.win.webContents.getZoomFactor();
    const key=JSON.stringify([enabled,frame,width,height,zoom,scale,this.blocked]);
    if(!force&&this.last===key)return;
    this.last=key;const generation=++this.generation;
    if(this.timer)clearTimeout(this.timer);this.timer=null;
    if(force)this.surface.invalidate(this.win);
    const wanted=enabled&&!this.blocked;
    if(!wanted&&!this.nativeApplied){this.report({active:false,clippingCheck:"not_run",materialCheck:"not_run"});return;}
    this.shape=frame?frameShape(width!,height!,zoom,frame):null;
    if(wanted&&!this.shape){this.report({active:false,clippingCheck:"checking",materialCheck:"not_run"});return;}
    try {this.surface.apply(this.win,wanted,scale,frame??undefined,true);if(wanted)this.nativeApplied=true;}
    catch {this.report({active:false,clippingCheck:"fail",materialCheck:"fail",fallbackReason:"native_apply_failed"});this.blocked=true;this.unsafe();return;}
    if(!this.shape){this.report({active:false,clippingCheck:"not_run",materialCheck:"not_run"});return;}
    this.report({active:false,clippingCheck:"checking",materialCheck:"checking"});
    const shape=this.shape;
    this.timer=setTimeout(()=>{this.timer=null;void this.verify(generation,shape,wanted);},100);
  }
  private current(generation:number) {return !this.disposed&&generation===this.generation;}
  private async verify(generation:number,shape:ShapeRect[],wanted:boolean) {
    let result:NativeOverlayCheck;
    try {result=await this.probe(shape,wanted);}catch {result={regionVerified:false,materialVerified:false,desktop:"not_run"};}
    if(!this.current(generation))return;
    if(result.regionVerified&&result.materialVerified){
      this.report({active:wanted,clippingCheck:"pass",materialCheck:"pass",fallbackReason:this.blocked?(this.evidence.fallbackReason??null):null});return;
    }
    this.blocked=true;
    this.report({active:false,clippingCheck:result.regionVerified?"pass":"fail",materialCheck:result.materialVerified?"pass":"fail",fallbackReason:"native_verification_failed"});
    try {
      // Keep the existing region until removal is read back; never expose an acrylic rectangle.
      this.surface.apply(this.win,false,1,undefined,true);
      const fallback=await this.probe(shape,false);
      if(!this.current(generation))return;
      if(fallback.regionVerified&&fallback.materialVerified){
        this.report({active:false,clippingCheck:"pass",materialCheck:"pass",fallbackReason:"native_verification_failed"});return;
      }
    }catch { /* Only this owner's plain fallback may replace an unverified native surface. */ }
    if(this.current(generation))this.unsafe();
  }
  dispose(){this.disposed=true;this.generation++;if(this.timer)clearTimeout(this.timer);this.timer=null;}
}
