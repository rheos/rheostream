// The CSS Modules import shape. apps/web gets this from Next.js's own type
// references; this package is compiled by the shell's Next build but typechecked on
// its own, so it declares the same shape here.
declare module "*.module.css" {
  const classes: Readonly<Record<string, string>>;
  export default classes;
}
