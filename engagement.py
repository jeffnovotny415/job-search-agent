"""Deterministic routing: a contract staff position is still a role."""
import re

ROLES = 'Watching — Roles'
PROJECTS = 'Watching — Projects'


def track(job):
    if job.get('engagement_track') in {'Roles', 'Projects'}:
        return job['engagement_track']
    title = job.get('title', '')
    kind = job.get('opportunity_type', '')
    employment = job.get('employment_type', '')
    if kind == 'RFP' or job.get('compensation_unit') == 'project':
        return 'Projects'
    if re.search(r'\b(?:full[- ]time|part[- ]time|fixed[- ]term|staff|temporary)\b', employment, re.I):
        return 'Roles'
    if re.search(r'\b(?:website|web app|woocommerce|wordpress|integration|automation)\b', title, re.I) and re.search(r'\b(?:new .{0,20}project|one[- ]off|redesign|build)\b', title, re.I):
        return 'Projects'
    if re.search(r'\b(?:RFP|request for proposals?|website redesign|website build|project[- ]based|fixed[- ](?:fee|price))\b', title, re.I):
        return 'Projects'
    # Descriptions of salaried roles often mention projects they will own.
    if kind == 'Contract' and re.search(r'\b(?:project[- ]based|fixed[- ](?:fee|price)|seeking (?:an? )?(?:agency|vendor)|scope of work|submit (?:a |your )?proposal)\b', job.get('description', ''), re.I):
        return 'Projects'
    return 'Roles'


def project_card(card, list_name=''):
    return (list_name == PROJECTS or '**Track:** Projects' in card.get('desc', '')
            or bool(re.search(r'—\s*\[RFP\]', card.get('name', ''))))
