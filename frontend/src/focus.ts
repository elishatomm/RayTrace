import { type RefObject, useLayoutEffect } from "react";

// A control that goes away, or is disabled, once its own request is answered would drop a keyboard user to the page
// body. If it holds focus, `target` takes it first; focus the user has moved anywhere else stays where it is.
export function handOverFocus(control: HTMLElement | null, target: HTMLElement | null): void {
  if (control !== null && control === document.activeElement) {
    target?.focus();
  }
}

// Logging in or out swaps the page's controls, and the control that held focus goes with them, which would leave a
// keyboard user on the page body. App keeps the element that held focus when the swap began. The control that
// replaces it takes focus on mount, but only if that element is gone and focus is on the body: focus the user had
// already put on the page itself stays there.
export function useFocusAfterSwap(focusBefore: RefObject<Element | null>, target: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    const before = focusBefore.current;
    focusBefore.current = null;
    const onBody = document.activeElement === null || document.activeElement === document.body;
    if (before !== null && before !== document.body && !before.isConnected && onBody) {
      target.current?.focus();
    }
  }, [focusBefore, target]);
}
