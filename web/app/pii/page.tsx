import { HeaderStrip } from "@/components/dashboard/HeaderStrip";
import { Matrix } from "@/components/pii/Matrix";

export default function PiiPage() {
  return (
    <main className="relative min-h-screen bg-(--surface-base) overflow-hidden pb-16">
      {/* Visual background grid backdrop */}
      <div className="absolute inset-0 grid-bg pointer-events-none" />

      {/* Decorative background radial glows */}
      <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-[#3ECF8E]/8 rounded-full blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-[#3ECF8E]/4 rounded-full blur-[120px] pointer-events-none" />

      <div className="relative z-10">
        <HeaderStrip surface="pii ledger" sourceConnectionId="demo-source" />
        <Matrix sourceId="demo-source" />
      </div>
    </main>
  );
}

