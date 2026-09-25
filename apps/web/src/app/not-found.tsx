import panel from "@/shell/panel.module.css";

/**
 * Next's not-found boundary: an unmatched route, or a `notFound()` call, renders
 * this inside the root layout rather than a blank or unstyled page.
 *
 * It links nowhere. The shell's root is built from the routing configuration,
 * which is request-time data, and Next prerenders this page at build time, when
 * that configuration is unavailable; a link baked in then would be wrong in one
 * of the two topologies.
 */
export default function NotFound() {
  return (
    <main className={panel.page}>
      <section className={panel.panel}>
        <h1 className={panel.heading}>Page not found</h1>
        <p className={panel.line}>There is nothing at this address.</p>
      </section>
    </main>
  );
}
