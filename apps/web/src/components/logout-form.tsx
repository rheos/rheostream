import styles from "./logout-form.module.css";

/**
 * The sign-out form: a plain HTML POST, no JavaScript (C9).
 *
 * `POST /auth/logout` takes no body and answers with a redirect, so an ordinary
 * form submission is the whole interaction — and a form navigation sends the
 * `Origin` header that the route's CSRF guard requires. `action` is always a
 * same-host path from `logoutAction`; an absolute URL would send a subdomain-mode
 * POST to the identity host, which does not hold this host's session cookie.
 */
export function LogoutForm({ action }: { action: string }) {
  return (
    <form className={styles.form} method="post" action={action}>
      <button className={styles.button} type="submit">
        Sign out
      </button>
    </form>
  );
}
