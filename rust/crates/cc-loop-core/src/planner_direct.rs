//! Auto-direct planner: skip planner provider for short/simple goals.

use crate::config::LoopConfig;

const SIMPLE_KEYWORDS: &[&str] = &[
    "fix", "bug", "cli", "docs", "typo", "test", "minimal change", "readme",
];
const COMPLEX_KEYWORDS: &[&str] = &[
    "architecture", "migration", "redesign", "refactor all", "rewrite",
];

pub fn should_skip_planner(goal: &str, config: &LoopConfig) -> bool {
    if !config.auto_direct_planner {
        return false;
    }
    if config.planner_mode != "auto" && config.planner_mode != "direct" {
        return false;
    }
    if config.planner_mode == "direct" {
        return true;
    }
    let g = goal.to_lowercase();
    if COMPLEX_KEYWORDS.iter().any(|k| g.contains(k)) {
        return false;
    }
    if goal.chars().count() > config.auto_direct_max_goal_chars {
        return false;
    }
    SIMPLE_KEYWORDS.iter().any(|k| g.contains(k))
}

pub fn direct_plan_json(goal: &str) -> serde_json::Value {
    serde_json::json!({
        "mode": "task_graph",
        "summary": goal.chars().take(240).collect::<String>(),
        "nodes": [{
            "id": "n1",
            "title": "deliver",
            "goal": goal,
            "depends_on": []
        }],
        "auto_direct": true,
        "implementer_prompt": goal,
    })
}
