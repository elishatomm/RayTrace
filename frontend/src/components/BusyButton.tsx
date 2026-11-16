import { type ReactNode, type Ref, useState } from "react";

// "primary" marks the action a card exists for (the next safe step); every other button is secondary. A busy or
// disabled button of either kind looks the same: see styles.css.
export type ButtonVariant = "primary" | "secondary";

interface BusyButtonProps {
  busy: boolean; // a request the button waits on is running
  disabled: boolean; // any other reason the button cannot be used
  onClick: () => void;
  ref?: Ref<HTMLButtonElement>; // for a caller that moves focus on when the button goes away
  variant?: ButtonVariant;
  children: ReactNode;
}

// A disabled element loses keyboard focus, so a button that disabled itself while its own request ran dropped a
// keyboard user to the page body on every press. While busy the button stays focusable instead: aria-disabled
// announces it as unavailable, and the click is ignored. Native `disabled` stays for every other reason.
export function BusyButton({ busy, disabled, onClick, ref, variant = "secondary", children }: Readonly<BusyButtonProps>) {
  return (
    <button
      ref={ref}
      type="button"
      className={variant === "primary" ? "primary" : undefined}
      disabled={disabled}
      aria-disabled={busy || undefined}
      onClick={busy ? undefined : onClick}
    >
      {children}
    </button>
  );
}

interface RequestButtonProps {
  disabled?: boolean; // any reason other than a request that the button cannot be used
  busy?: boolean; // busy beyond its own request, such as while the job that request started runs
  onClick: () => Promise<void>; // sends the button's own request; settles once its answer is handled
  ref?: Ref<HTMLButtonElement>;
  variant?: ButtonVariant;
  children: ReactNode;
}

// A BusyButton that is busy while the request its own press sent is running, so a quick second press sends nothing
// and focus stays. Only its own request counts: a request started anywhere else on the page leaves it available.
export function RequestButton({ disabled = false, busy = false, onClick, ref, variant, children }: Readonly<RequestButtonProps>) {
  const [requesting, setRequesting] = useState(false);

  async function press() {
    setRequesting(true);
    try {
      await onClick();
    } finally {
      setRequesting(false);
    }
  }

  return (
    <BusyButton ref={ref} busy={requesting || busy} disabled={disabled} variant={variant} onClick={() => void press()}>
      {children}
    </BusyButton>
  );
}
