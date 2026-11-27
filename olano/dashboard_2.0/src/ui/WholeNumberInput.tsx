import { useEffect, useState, type CSSProperties } from "react";
import { parseWholeNumber, textShowsValue } from "./wholeNumber";

// A number field that can be emptied while editing (see ui/wholeNumber.ts).
// `onChange` always receives a validated whole number; the box shows what the
// person typed (empty included), and shows the saved number again once it
// loses focus.
export function WholeNumberInput({
  value,
  onChange,
  fallback = 0,
  className = "input",
  style,
  min = 0,
  ...rest
}: {
  value: number;
  onChange: (n: number) => void;
  fallback?: number;
  className?: string;
  style?: CSSProperties;
  min?: number;
  "aria-label"?: string;
  id?: string;
}) {
  const [text, setText] = useState(String(value));
  // A value changed from outside (a reload of the saved config): show it.
  useEffect(() => {
    setText((cur) => (textShowsValue(cur, value, fallback) ? cur : String(value)));
  }, [value, fallback]);
  return (
    <input
      {...rest}
      type="number"
      inputMode="numeric"
      className={className}
      style={style}
      min={min}
      step={1}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(parseWholeNumber(e.target.value, fallback));
      }}
      onBlur={() => setText((cur) => String(parseWholeNumber(cur, fallback)))}
    />
  );
}
