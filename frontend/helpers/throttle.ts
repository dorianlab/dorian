export const throttle = <T extends (...args: any[]) => void>(
  fn: T,
  ms = 300
) => {
  let last = 0,
    t: any = null,
    q: any[] | null = null;

  return (...args: Parameters<T>) => {
    const now = Date.now();
    const run = () => {
      last = Date.now();
      t = null;
      fn(...(q ?? args));
      q = null;
    };

    if (now - last >= ms) run();
    else {
      q = args;
      clearTimeout(t);
      t = setTimeout(run, ms - (now - last));
    }
  };
};
