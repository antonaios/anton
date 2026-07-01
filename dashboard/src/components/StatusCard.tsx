interface Props {
  title: string;
  primary: string;
  sub: string;
  pct: number;
  variant?: "info" | "green";
}

const GRADIENT: Record<"info" | "green", string> = {
  info:  "radial-gradient(ellipse at 10% 10%, rgba(106,140,175,0.08) 0%, #0d0f10 62%)",
  green: "radial-gradient(ellipse at 10% 90%, rgba(107,176,131,0.07) 0%, #0d0f10 62%)",
};

export function StatusCard({ title, primary, sub, pct, variant }: Props) {
  return (
    <div
      className="panel flex flex-col px-[13px] py-[10px]"
      style={variant ? { background: GRADIENT[variant] } : undefined}
    >
      <div className="text-[9.5px] uppercase tracking-[0.16em] text-label">{title}</div>
      <div className="mono mt-1 text-[13px] font-medium text-text-primary">{primary}</div>
      <div className="mt-[5px] h-0.5 w-full overflow-hidden bg-line-strong">
        <div className="h-full bg-info" style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
      <div className="mono mt-2 text-[10px] text-text-secondary">{sub}</div>
    </div>
  );
}
