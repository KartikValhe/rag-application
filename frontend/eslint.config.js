import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";

export default [
  {
    ignores: ["dist/**", "node_modules/**", "**/*.config.js", "**/*.config.ts"],
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: {
          jsx: true,
        },
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": ["warn", { "argsIgnorePattern": "^_", "varsIgnorePattern": "^_" }],
      "no-console": ["warn", { "allow": ["warn", "error"] }],
      "no-restricted-syntax": [
        "error",
        {
          "selector": "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          "message": "dangerouslySetInnerHTML is strictly forbidden in src/ for security reasons."
        },
        {
          "selector": "Identifier[name='dangerouslySetInnerHTML']",
          "message": "dangerouslySetInnerHTML is strictly forbidden in src/ for security reasons."
        }
      ],
    },
  },
];

