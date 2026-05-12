<?php

$config['product_name'] = 'Gmail';
$config['support_url'] = '';

$config['layout'] = 'widescreen';

$config['session_lifetime'] = 1440;
$config['auto_create_user'] = true;

$config['ip_check'] = false;

$config['default_charset'] = 'UTF-8';
$config['htmleditor'] = 0;
$config['draft_autosave'] = 0;

$config['enable_spellcheck'] = false;

$config['identities_level'] = 0;

$config['disabled_actions'] = [];

$config['plugins'] = array_merge(
    (array)$config['plugins'],
    array('account_signup')
);
