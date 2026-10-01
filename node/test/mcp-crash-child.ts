import { main } from "../src/mcp.ts";
import { hooks } from "../src/internal-hooks.ts";

hooks.afterCreateRevisionInsert = () => process.exit(95);
await main(process.argv.slice(2));
