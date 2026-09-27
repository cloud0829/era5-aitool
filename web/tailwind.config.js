/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef6ff",
          100: "#d9eaff",
          200: "#bcd9ff",
          300: "#8ec2ff",
          400: "#5aa0ff",
          500: "#357efb",
          600: "#1f5ff0",
          700: "#174ad0",
          800: "#173ea8",
          900: "#183884",
        },
      },
    },
  },
  plugins: [],
};
