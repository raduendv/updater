# {{ app_title }} activity

**Source:** `{{ source_repo }}`  
**Reporting window:** {{ period_start }} - {{ period_end }}

{% if issues %}
{% for issue in issues %}
## [#{{ issue.number }} {{ issue.title | markdown_escape }}]({{ issue.url }})

**Current state:** {{ issue.state | markdown_escape }}  
**Assignees:** {% if issue.assignees %}{{ issue.assignees | map('mention') | join(', ') }}{% else %}none{% endif %}  
**Milestone:** {% if issue.milestone %}[{{ issue.milestone.title | markdown_escape }}]({{ issue.milestone.url }}){% else %}none{% endif %}  
**Labels:** {% if issue.labels %}{% for label in issue.labels %}[{{ label.name | markdown_escape }}]({{ label.url }}){% if not loop.last %}, {% endif %}{% endfor %}{% else %}none{% endif %}

{% if issue.events %}
### Changes
{% for event in issue.events %}
{% if event.sub_issue %}
- Sub-issue {{ event.sub_issue.action }} by @{{ event.actor | markdown_escape }}: [#{{ event.sub_issue.number }} {{ event.sub_issue.title | markdown_escape }}]({{ event.sub_issue.url }}) ({{ event.created_at }})
{% else %}
- {{ event.summary | markdown_escape }} ({{ event.created_at }})
{% endif %}
{% endfor %}
{% endif %}

{% if issue.comments %}
### Comments
{% for comment in issue.comments %}
#### {{ comment.kind | markdown_escape }} comment by {{ comment.author | markdown_escape }}

{{ comment.timestamps }} · [Open source comment]({{ comment.url }})

{{ comment.body | code_block }}
{% endfor %}
{% endif %}
{% endfor %}
{% else %}
No source activity was found in this reporting window.
{% endif %}

---
{{ marker }}
