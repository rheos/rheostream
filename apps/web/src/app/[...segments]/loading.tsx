import panel from "@/shell/panel.module.css";

/**
 * What a module screen shows while its server render is still reading core. It
 * sits inside the root layout only, not the frame: the frame's navigation depends
 * on the same reads the screen is waiting for.
 */
export default function Loading() {
  return (
    <main className={panel.page}>
      <section className={panel.panel} role="status" aria-live="polite">
        <p className={panel.line}>Loading…</p>
      </section>
    </main>
  );
}
