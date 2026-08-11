import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        border: "hsl(220 13% 18%)",
        input: "hsl(220 13% 18%)",
        ring: "hsl(220 13% 40%)",
        background: "hsl(220 13% 6%)",
        foreground: "hsl(220 13% 90%)",
        primary: {
          DEFAULT: "hsl(220 80% 60%)",
          foreground: "hsl(0 0% 100%)",
        },
        secondary: {
          DEFAULT: "hsl(220 13% 14%)",
          foreground: "hsl(220 13% 70%)",
        },
        destructive: {
          DEFAULT: "hsl(0 62% 50%)",
          foreground: "hsl(0 0% 100%)",
        },
        muted: {
          DEFAULT: "hsl(220 13% 14%)",
          foreground: "hsl(220 13% 50%)",
        },
        accent: {
          DEFAULT: "hsl(220 50% 20%)",
          foreground: "hsl(220 13% 90%)",
        },
        card: {
          DEFAULT: "hsl(220 13% 10%)",
          foreground: "hsl(220 13% 90%)",
        },
        positive: "hsl(160 60% 45%)",
        negative: "hsl(0 62% 50%)",
        warning: "hsl(40 80% 50%)",
      },
      borderRadius: {
        lg: "0.5rem",
        md: "0.375rem",
        sm: "0.25rem",
      },
    },
  },
  plugins: [],
};
export default config;
